#!/usr/bin/env python

# Copyright 2019 Anantak Robotics Inc.

# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# [START speech_transcribe_infinite_streaming]
from __future__ import division

import time
import re
import sys
import zmq
import json

from google.cloud import speech

import pyaudio
from six.moves import queue

# Audio recording parameters
STREAMING_LIMIT = 55000
SAMPLE_RATE = 16000
CHUNK_SIZE = int(SAMPLE_RATE / 10)  # 10 100ms

commands = ['go short',
            'go medium',
            'go long',
            'go slow',
            'go fast',
            'go run',
            'go manual',
            'go stop',
            'go auto',
            'stop',
            'halt']

command_dicts = [{'move': {'speed':1.0, 'duration':0.0, 'distance':0.5}},
                 {'move': {'speed':1.0, 'duration':0.0, 'distance':1.5}},
                 {'move': {'speed':1.0, 'duration':0.0, 'distance':2.5}},
                 {'move': {'speed':0.6, 'duration':7200.0, 'distance':0.0}},
                 {'move': {'speed':1.0, 'duration':7200.0, 'distance':0.0}},
                 {'move': {'speed':1.1, 'duration':7200.0, 'distance':0.0}},
                 {'terminate':1},
                 {'move': {'speed':0.0, 'duration':0.0, 'distance':0.0}},
                 {'move': {'speed':0.0, 'duration':0.0, 'distance':0.0}},
                 {'move': {'speed':0.0, 'duration':0.0, 'distance':0.0}},
                 {'move': {'speed':0.0, 'duration':0.0, 'distance':0.0}}]

# ZMQ_READ_PORT = 7777
ZMQ_SEND_PORT = 7781

last_command_ts = 0
last_command_num = -1

def get_current_time():
    return int(round(time.time() * 1000))


def duration_to_secs(duration):
    return duration.seconds + (duration.nanos / float(1e9))


class ResumableMicrophoneStream:
    """Opens a recording stream as a generator yielding the audio chunks."""
    def __init__(self, rate, chunk_size):
        self._rate = rate
        self._chunk_size = chunk_size
        self._num_channels = 1
        self._max_replay_secs = 5

        # Create a thread-safe buffer of audio data
        self._buff = queue.Queue()
        self.closed = True
        self.start_time = get_current_time()

        # 2 bytes in 16 bit samples
        self._bytes_per_sample = 2 * self._num_channels
        self._bytes_per_second = self._rate * self._bytes_per_sample

        self._bytes_per_chunk = (self._chunk_size * self._bytes_per_sample)
        self._chunks_per_second = (
                self._bytes_per_second // self._bytes_per_chunk)

    def __enter__(self):
        self.closed = False

        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=self._num_channels,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk_size,
            # Run the audio stream asynchronously to fill the buffer object.
            # This is necessary so that the input device's buffer doesn't
            # overflow while the calling thread makes network requests, etc.
            stream_callback=self._fill_buffer,
        )

        return self

    def __exit__(self, type, value, traceback):
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        # Signal the generator to terminate so that the client's
        # streaming_recognize method will not block the process termination.
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, *args, **kwargs):
        """Continuously collect data from the audio stream, into the buffer."""
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        while not self.closed:
            if get_current_time() - self.start_time > STREAMING_LIMIT:
                self.start_time = get_current_time()
                break
            # Use a blocking get() to ensure there's at least one chunk of
            # data, and stop iteration if the chunk is None, indicating the
            # end of the audio stream.
            chunk = self._buff.get()
            if chunk is None:
                return
            data = [chunk]

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break

            yield b''.join(data)


def listen_print_loop(responses, stream, sock_send):
    """Iterates through server responses and prints them.

    The responses passed is a generator that will block until a response
    is provided by the server.

    Each response may contain multiple results, and each result may contain
    multiple alternatives; for details, see https://goo.gl/tjCPAU.  Here we
    print only the transcription for the top alternative of the top result.

    In this case, responses are provided for interim results as well. If the
    response is an interim one, print a line feed at the end of it, to allow
    the next result to overwrite it, until the response is a final one. For the
    final one, print a newline to preserve the finalized transcription.
    """

    global last_command_ts
    global last_command_num

    try:
        responses = (r for r in responses if (
                r.results and r.results[0].alternatives))

        num_chars_printed = 0
        for response in responses:
            if not response.results:
                continue

            # The `results` list is consecutive. For streaming, we only care about
            # the first result being considered, since once it's `is_final`, it
            # moves on to considering the next utterance.
            result = response.results[0]
            if not result.alternatives:
                continue

            # Display the transcription of the top alternative.
            top_alternative = result.alternatives[0]
            transcript = top_alternative.transcript

            accept = False
            if (result.stability > 0.0) or result.is_final:
                accept = True

            if (result.stability > 0.0):
                stability_str = ' %.2f' % result.stability
                transcript += stability_str
            if (result.is_final):
                transcript += ' Final'

            # Display interim results, but with a carriage return at the end of the
            # line, so subsequent lines will overwrite them.
            #
            # If the previous result was longer than this one, we need to print
            # some extra spaces to overwrite the previous result
            overwrite_chars = ' ' * (num_chars_printed - len(transcript))

            # if not result.is_final:
            if not accept:
                sys.stdout.write(transcript + overwrite_chars + '\r')
                sys.stdout.flush()

                num_chars_printed = len(transcript)
            else:
                print(transcript + overwrite_chars)

                # Exit recognition if any of the transcribed phrases could be
                # one of our keywords.
                if re.search(r'\b(exit|quit)\b', transcript, re.I):
                    print('Exiting..')
                    stream.closed = True
                    break

                command_idx = -1
                for i in range(len(commands)):
                    if re.search(r'\b('+commands[i]+r')\b', transcript, re.I):
                        print '   Command: ', commands[i]
                        command_idx = i

                if (command_idx > -1):
                    # print 'Publishing command', command_idx

                    curr_command_ts = time.time()

                    # Check if running commands are being repeated too fast
                    send_command = True
                    if (last_command_num == command_idx):
                        if (command_idx < 6):
                            if (curr_command_ts - last_command_ts < 2):
                                send_command = False

                    if (send_command):
                        voice_msg = {}
                        voice_msg['handheld'] = command_dicts[command_idx]
                        voice_msg_str = json.dumps(voice_msg)

                        print '   Sending: ', voice_msg_str
                        sock_send.send(voice_msg_str)
                        last_command_ts = curr_command_ts
                        last_command_num = command_idx

                    else:
                        print '   Too soon to repeat command'

                num_chars_printed = 0

    except Exception as inst:
        print(type(inst))
        print(inst.args)
        print(inst)
        raise

"""
single_utterance - (optional, defaults to false) indicates whether this request should automatically
end after speech is no longer detected. If set, Speech-to-Text will detect pauses, silence, or non-speech
audio to determine when to end recognition. If not set, the stream will continue to listen and process
audio until either the stream is closed directly, or the stream's limit length has been exceeded.
Setting single_utterance to true is useful for processing voice commands.
"""

def main():
    client = speech.SpeechClient()
    config = speech.types.RecognitionConfig(
        encoding=speech.enums.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code='en-US',
        speech_contexts=[speech.types.SpeechContext(phrases=commands)],
        max_alternatives=1,
        enable_word_time_offsets=False,
        # Enhanced models are only available to projects that opt in for audio data collection.
        # A model must be specified to use enhanced model.
        use_enhanced=True,
        model='command_and_search'
        )
    streaming_config = speech.types.StreamingRecognitionConfig(
        config=config,
        single_utterance=False,
        interim_results=True)

    print('Say "Quit" or "Exit" to terminate the program.')

    zmq_context = zmq.Context()
    sock_send = zmq_context.socket(zmq.PUB)
    sock_send.bind('tcp://*:%d' % ZMQ_SEND_PORT)

    while (True):

        try:

            mic_manager = ResumableMicrophoneStream(SAMPLE_RATE, CHUNK_SIZE)

            with mic_manager as stream:
                while not stream.closed:
                    print('starting stream')
                    audio_generator = stream.generator()
                    requests = (speech.types.StreamingRecognizeRequest(
                        audio_content=content)
                        for content in audio_generator)

                    responses = client.streaming_recognize(streaming_config,
                                                           requests)
                    # Now, put the transcription responses to use.
                    listen_print_loop(responses, stream, sock_send)

        except Exception as inst:

            print(type(inst))
            print(inst.args)
            print(inst)

if __name__ == '__main__':
    main()
# [END speech_transcribe_infinite_streaming]

import sys
import threading
import time
import zmq

ZMQ_READ_PORT = 7781

def main(argv):
    context = zmq.Context()
    sock_read = context.socket(zmq.SUB)
    sock_read.setsockopt(zmq.SUBSCRIBE, '')
    sock_read.setsockopt(zmq.CONFLATE, 1)
    sock_read.connect('tcp://127.0.0.1:%d' % ZMQ_READ_PORT)

    while (True):

        try:
            out_msg = sock_read.recv(zmq.NOBLOCK)
            print('Got: %s' % (out_msg))
        except zmq.Again as e:
            print('No message received yet')

        time.sleep(1)


if __name__ == '__main__':
    main(sys.argv)

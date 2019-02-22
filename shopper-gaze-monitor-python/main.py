"""Shopper Gaze Monitor."""

"""
 Copyright (c) 2018 Intel Corporation.
 Permission is hereby granted, free of charge, to any person obtaining
 a copy of this software and associated documentation files (the
 "Software"), to deal in the Software without restriction, including
 without limitation the rights to use, copy, modify, merge, publish,
 distribute, sublicense, and/or sell copies of the Software, and to
 permit person to whom the Software is furnished to do so, subject to
 the following conditions:
 The above copyright notice and this permission notice shall be
 included in all copies or substantial portions of the Software.
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
 LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import os
import sys
import json
import time
import cv2

import logging as log
import paho.mqtt.client as mqtt

from threading import Thread
from collections import namedtuple
from argparse import ArgumentParser
from inference import Network

# shoppingInfo contains statistics for the shopping information
MyStruct = namedtuple("shoppingInfo", "shopper, looker")
INFO = MyStruct(0, 0)

POSE_CHECKED = False

# MQTT server environment variables
TOPIC = "shopper_gaze_monitor"
MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_KEEPALIVE_INTERVAL = 60

# Flag to control background thread
KEEP_RUNNING = True

DELAY = 5


def args_parser():
    """
    Parse command line arguments.
    :return: Command line arguments
    """
    parser = ArgumentParser()
    parser.add_argument("-m", "--model", required=True,
                        help="Path to an .xml file with a pre-trained"
                        "face detection model")
    parser.add_argument("-pm", "--posemodel", required=True,
                        help="Path to an .xml file with a pre-trained model"
                        "head pose model")
    parser.add_argument("-i", "--input", required=True, type=str,
                        help="Path to video file or image."
                        "'cam' for capturing video stream from camera")
    parser.add_argument("-l", "--cpu_extension", type=str, default=None,
                        help="MKLDNN (CPU)-targeted custom layers. Absolute "
                        "path to a shared library with the kernels impl.")
    parser.add_argument("-d", "--device", default="CPU", type=str,
                        help="Specify the target device to infer on; "
                        "CPU, GPU, FPGA or MYRIAD is acceptable. Looks"
                        "for a suitable plugin for device specified"
                        "(CPU by default)")
    parser.add_argument("-c", "--confidence", default=0.5, type=float,
                        help="Probability threshold for detections filtering")

    return parser


def face_detection(res, args, initial_wh):
    """
    Parse Face detection output.
    :param res: Detection results
    :param args: Parsed arguments
    :param initial_wh: Initial width and height of the FRAME
    :return: Co-ordinates of the detected face
    """
    global INFO
    faces = []
    INFO = INFO._replace(shopper=0)

    for obj in res[0][0]:
        # Draw only objects when probability more than specified threshold
        if obj[2] > args.confidence:
            if obj[3] < 0:
                obj[3] = -obj[3]
            if obj[4] < 0:
                obj[4] = -obj[4]
            xmin = int(obj[3] * initial_wh[0])
            ymin = int(obj[4] * initial_wh[1])
            xmax = int(obj[5] * initial_wh[0])
            ymax = int(obj[6] * initial_wh[1])
            faces.append([xmin, ymin, xmax, ymax])
            INFO = INFO._replace(shopper=len(faces))
    return faces


def message_runner():
    """
    Publish worker status to MQTT topic.
    Pauses for rate second(s) between updates
    :return: None
    """
    while KEEP_RUNNING:
        payload = json.dumps({"Shopper": INFO.shopper, "Looker": INFO.looker})
        time.sleep(1)
        CLIENT.publish(TOPIC, payload=payload)


def main():
    """
    Load the network and parse the output.
    :return: None
    """
    global INFO
    global DELAY
    global CLIENT
    global KEEP_RUNNING
    global POSE_CHECKED

    CLIENT = mqtt.Client()
    CLIENT.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE_INTERVAL)

    log.basicConfig(format="[ %(levelname)s ] %(message)s",
                    level=log.INFO, stream=sys.stdout)
    args = args_parser().parse_args()
    logger = log.getLogger()

    if args.input == 'cam':
        input_stream = 0
    else:
        input_stream = args.input

    cap = cv2.VideoCapture(input_stream)

    if input_stream:
        cap.open(args.input)
        # Adjust DELAY to match the number of FPS of the video file
        DELAY = 1000 / cap.get(cv2.CAP_PROP_FPS)

    if not cap.isOpened():
        logger.error("ERROR! Unable to open video source")
        return

    framerate=25
    display="appsrc ! videoconvert ! matroskamux streamable=true ! tcpserversink host=0.0.0.0 port=6000 sync=false sync-method=2"
    fourcc = 0
    displayout = cv2.VideoWriter(display, fourcc, framerate, (640, 480))

    # Initialise the class
    infer_network = Network()
    infer_network_pose = Network()
    # Load the network to IE plugin to get shape of input layer
    plugin, (n_fd, c_fd, h_fd, w_fd) = infer_network.load_model(args.model,
                                                      args.device, 1, 1, 0,
                                                      args.cpu_extension)
    n_hp, c_hp, h_hp, w_hp = infer_network_pose.load_model(args.posemodel,
                                                           args.device, 1,
                                                           3, 0,
                                                           args.cpu_extension, plugin)[1]
    message_thread = Thread(target=message_runner)
    message_thread.setDaemon(True)
    message_thread.start()

    ret, frame = cap.read()
    while ret:

        looking = 0
        ret, next_frame = cap.read()
        if not ret:
            KEEP_RUNNING = False
            break

        if next_frame is None:
            KEEP_RUNNING = False
            log.error("ERROR! blank FRAME grabbed")
            break

        initial_wh = [cap.get(3), cap.get(4)]
        in_frame_fd = cv2.resize(next_frame, (w_fd, h_fd))
        # Change data layout from HWC to CHW
        in_frame_fd = in_frame_fd.transpose((2, 0, 1))
        in_frame_fd = in_frame_fd.reshape((n_fd, c_fd, h_fd, w_fd))

        key_pressed = cv2.waitKey(int(DELAY))

        # Start asynchronous inference for specified request
        inf_start_fd = time.time()
        infer_network.exec_net(0, in_frame_fd)
        # Wait for the result
        infer_network.wait(0)
        det_time_fd = time.time() - inf_start_fd

        # Results of the output layer of the network
        res = infer_network.get_output(0)

        # Parse face detection output
        faces = face_detection(res, args, initial_wh)

        if len(faces) != 0:
            # Look for poses
            for res_hp in faces:
                xmin, ymin, xmax, ymax = res_hp
                head_pose = frame[ymin:ymax, xmin:xmax]
                in_frame_hp = cv2.resize(head_pose, (w_hp, h_hp))
                in_frame_hp = in_frame_hp.transpose((2, 0, 1))
                in_frame_hp = in_frame_hp.reshape((n_hp, c_hp, h_hp, w_hp))

                inf_start_hp = time.time()
                infer_network_pose.exec_net(0, in_frame_hp)
                infer_network_pose.wait(0)
                det_time_hp = time.time() - inf_start_hp


                # Parse head pose detection results
                angle_p_fc = infer_network_pose.get_output(0, "angle_p_fc")
                angle_y_fc = infer_network_pose.get_output(0, "angle_y_fc")
                if ((angle_y_fc > -22.5) & (angle_y_fc < 22.5) & (angle_p_fc > -22.5) &
                        (angle_p_fc < 22.5)):
                    looking += 1
                    POSE_CHECKED = True
                    INFO = INFO._replace(looker=looking)
                else:
                    INFO = INFO._replace(looker=looking)
        else:
            INFO = INFO._replace(looker=0)

        # Draw performance stats
        inf_time_message = "Face Inference time: {:.3f} ms.".format(det_time_fd * 1000)

        if POSE_CHECKED:
            cv2.putText(frame, "Head pose Inference time: {:.3f} ms.".format(det_time_hp * 1000), (0, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, inf_time_message, (0, 15), cv2.FONT_HERSHEY_COMPLEX,
                    0.5, (255, 255, 255), 1)
        cv2.putText(frame, "Shopper: {}".format(INFO.shopper), (0, 90), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1)
        cv2.putText(frame, "Looker: {}".format(INFO.looker), (0, 110), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1)

        cv2.imshow("Shopper Gaze Monitor", frame)
        displayout.write(frame)

        frame = next_frame

        if key_pressed == 27:
            print("Attempting to stop background threads")
            KEEP_RUNNING = False
            break
    infer_network.clean()
    infer_network_pose.clean()
    message_thread.join()
    cap.release()
    cv2.destroyAllWindows()
    CLIENT.disconnect()


if __name__ == '__main__':
    main()
    sys.exit()

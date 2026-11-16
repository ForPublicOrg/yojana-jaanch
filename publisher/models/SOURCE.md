# Detection models

`python run.py publish` runs two ONNX models with onnxruntime. The files are not stored in this
repository. On first use each is downloaded from its project's official GitHub release and checked
against the sha256 below; the publisher checks the sha256 again every time it loads a file and stops
if it does not match.

| Engine | File | Source | sha256 | Licence |
|---|---|---|---|---|
| Face: YOLOv12m-face (trained on WIDER FACE) | `yolov12m-face.onnx` (80816571 bytes) | https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov12m-face.onnx | `1d54b41c9e5dd2e428b841656eeef9ff5ebd8bca6a6d9381eb78d3eea902373e` | AGPL-3.0 (stated in the model file; the repository itself is GPL-3.0) |
| Head from pose: YOLO11x-pose (COCO keypoints) | `yolo11x-pose.onnx` (235496663 bytes) | https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11x-pose.onnx | `485164bd938c2d5e726336275dc8fd7ad04de495ebf3f8ec1ed15bde99f178c4` | AGPL-3.0 (Ultralytics) |

## Licence limits

Both models are AGPL-3.0. Using them to blur photos on your own computer is allowed, including for
commercial work. If you distribute the publisher together with the models, or offer it to others as a
network service, the AGPL asks you to share the source of that program under the same licence.
Ultralytics sells a separate enterprise licence for closed use.

## Engines measured and not used

These were measured on the same hand-labelled set (see README, "Publishing photos") and not chosen.
They are not downloaded by the publisher.

| Engine | Source | sha256 | Licence |
|---|---|---|---|
| YuNet 2023mar (the old detector, with Haar cascades) | https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | MIT |
| SCRFD-10GF (`det_10g.onnx` in insightface `buffalo_l.zip`) | https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip | `5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91` (det_10g.onnx) | non-commercial research use only |
| YOLOv12l-face | https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov12l-face.onnx | `d55f67bcddf5622cfc90306c6cb2f7d478e5ed4202a35dc2f833003c837243da` | AGPL-3.0 |
| YOLOv8n-face | https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov8n-face.onnx | `06b941fd5792be624ad18f2df9ede0a021c4df165dd418204d978c20fd555928` | AGPL-3.0 |
| YOLO11m-pose | https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11m-pose.onnx | `83bbd232abd824b52afa3747629605e88afb080c25641c6d03e7c736210c44dc` | AGPL-3.0 |
| MediaPipe BlazeFace full range | https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite | `3698b18f063835bc609069ef052228fbe86d9c9a6dc8dcb7c7c2d69aed2b181b` | Apache-2.0 |
| MediaPipe Pose Landmarker heavy | https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/1/pose_landmarker_heavy.task | `64437af838a65d18e5ba7a0d39b465540069bc8aae8308de3e318aad31fcbc7b` | Apache-2.0 |

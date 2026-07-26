import sys
from pathlib import Path

import cv2
import numpy as np

video_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
capture = cv2.VideoCapture(str(video_path))
fps = capture.get(cv2.CAP_PROP_FPS)
frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
duration = frame_count / fps if fps else 0

sample_count = 16
frames = []
for index, timestamp in enumerate(np.linspace(0, max(duration - 0.1, 0), sample_count)):
    capture.set(cv2.CAP_PROP_POS_MSEC, float(timestamp * 1000))
    ok, frame = capture.read()
    if not ok:
        continue
    target_width = 480
    target_height = max(1, round(frame.shape[0] * target_width / frame.shape[1]))
    frame = cv2.resize(frame, (target_width, target_height))
    cv2.rectangle(frame, (0, 0), (170, 30), (0, 0, 0), -1)
    cv2.putText(
        frame,
        f"{timestamp:07.2f}s",
        (8, 22),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    frames.append(frame)

cols = 4
rows = (len(frames) + cols - 1) // cols
tile_height = frames[0].shape[0]
sheet = np.zeros((rows * tile_height, cols * 480, 3), dtype=np.uint8)
for index, frame in enumerate(frames):
    row, col = divmod(index, cols)
    sheet[row * tile_height : (row + 1) * tile_height, col * 480 : (col + 1) * 480] = frame

cv2.imwrite(str(output_path), sheet)
print(f"duration={duration:.3f}")
print(f"fps={fps:.3f}")
print(f"frames={frame_count}")
print(f"resolution={width}x{height}")
print(f"contact_sheet={output_path}")

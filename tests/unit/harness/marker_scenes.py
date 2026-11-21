# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""A satellite map like Flight's (QA item 26), drawn for marker tests."""

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 1206, 2622  # iPhone 16 Pro screenshot
PIN_R = 17


def satellite_scene(seed: int = 7, packed: bool = False) -> tuple[bytes, dict]:
    """Fields, a lime job boundary, a grid of grey ring pins, one orange pin, a battery.

    Returns the PNG and what is in it: the cluster's pins and the orange pin.
    """
    rng = np.random.default_rng(seed)
    # Fields: patches of greens and browns, some vivid green, irregular and
    # textured like imagery (the old detector reported ~75 of them as pins).
    base = np.zeros((H // 8, W // 8, 3), np.float32)
    palette = np.array([[86, 110, 60], [120, 132, 74], [96, 84, 58], [70, 96, 52], [134, 118, 84]])
    base[:] = palette[rng.integers(0, len(palette), size=(H // 8, W // 8))]
    image = Image.fromarray(base.astype(np.uint8)).resize((W, H), Image.NEAREST)
    image = image.filter(ImageFilter.GaussianBlur(6))
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x, y = int(rng.integers(0, W)), int(rng.integers(150, H))
        size = int(rng.integers(40, 260))
        corners = [
            (
                x + int(np.cos(a) * size * rng.uniform(0.5, 1.0)),
                y + int(np.sin(a) * size * rng.uniform(0.5, 1.0)),
            )
            for a in np.sort(rng.uniform(0, 2 * np.pi, int(rng.integers(5, 9))))
        ]
        draw.polygon(corners, fill=(60, 185, 40))
    image = image.filter(ImageFilter.GaussianBlur(2))
    # Grain matched to real imagery: Apple satellite measures ~10 (mean |x - blur5|),
    # Flight's Mapbox 2-4; this gives ~10.
    texture = rng.normal(0, 14, size=(H, W, 1)).astype(np.float32)
    pixels = np.clip(np.asarray(image, np.float32) + texture, 0, 255).astype(np.uint8)
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    # The lime job boundary outline.
    boundary = [(60, 1000), (560, 900), (700, 1500), (240, 1700)]
    draw.line(boundary + boundary[:1], fill=(190, 255, 0), width=9)
    # The structure pins: grey circles with a white ring, packed in a cluster.
    cluster = []
    # packed=True overlaps them like Flight's real pack (centres 1.3 radii apart).
    step_x, step_y = (
        (int(1.3 * PIN_R), int(1.2 * PIN_R)) if packed else (2 * PIN_R + 4, 2 * PIN_R + 2)
    )
    for row in range(8):
        for col in range(10):
            cx, cy = 180 + col * step_x + (row % 2) * (step_x // 2), 1150 + row * step_y
            draw.ellipse([cx - PIN_R, cy - PIN_R, cx + PIN_R, cy + PIN_R], fill=(255, 255, 255))
            draw.ellipse(
                [cx - PIN_R + 5, cy - PIN_R + 5, cx + PIN_R - 5, cy + PIN_R - 5],
                fill=(128, 128, 132),
            )
            cluster.append((cx, cy))
    # Two lone grey pins and one orange pin.
    lone = [(900, 700), (1000, 2000)]
    for cx, cy in lone:
        draw.ellipse([cx - PIN_R, cy - PIN_R, cx + PIN_R, cy + PIN_R], fill=(255, 255, 255))
        draw.ellipse(
            [cx - PIN_R + 5, cy - PIN_R + 5, cx + PIN_R - 5, cy + PIN_R - 5], fill=(128, 128, 132)
        )
    orange = (850, 1380)
    # Like Flight's orange pin: a light rim around the fill.
    rim = [orange[0] - 22, orange[1] - 22, orange[0] + 22, orange[1] + 22]
    draw.ellipse(rim, fill=(240, 236, 228))
    fill = [orange[0] - 18, orange[1] - 18, orange[0] + 18, orange[1] + 18]
    draw.ellipse(fill, fill=(245, 130, 20))
    # Status bar: a green battery icon (it was a false hit).
    draw.rectangle([0, 0, W, 130], fill=(30, 40, 30))
    draw.rounded_rectangle([1040, 60, 1110, 95], radius=8, fill=(52, 199, 89))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), {"cluster": cluster, "lone": lone, "orange": orange}

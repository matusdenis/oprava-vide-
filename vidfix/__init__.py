"""VidFix - zachrana videi poskodenych ransomverom.

Balik obsahuje:
  util      - drobne pomocne funkcie (entropia, formatovanie, hexdump)
  mp4       - parser ISO BMFF (MP4/MOV) a primitiva na opravu
  carve     - vyrezavanie surovych streamov (H.264/HEVC NAL, MPEG-TS)
  headerdb  - databaza hlaviciek kontajnerov z verejnych zdrojov
  tools     - detekcia a spustanie ffmpeg / ffprobe / untrunc
  analyze   - diagnostika poskodeneho suboru
  repair    - jednotlive opravne strategie
  jobs      - beh uloh na pozadi pre UI
  server    - lokalne webove uzivatelske rozhranie
"""

__version__ = "1.0.0"

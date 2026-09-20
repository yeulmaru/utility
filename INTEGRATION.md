# 예울마루 콘텐츠 처리 작업

이 저장소는 공개 코드 저장소다. 미공개 영상, 내부 음성, 비밀 URL, 쿠키, 토큰은 커밋하거나 Actions 입력에 넣지 않는다.

## 구현 범위

- `utility_worker.py`: yt-dlp 다운로드 → FFmpeg MP4 표준화 → 출력 길이·소리 확인. faster-whisper large-v3 음성 인식 → 시간/문구 검증 → JSON/SRT/VTT 출력.
- `test_utility_worker.py`: URL 허용 범위, 한국어 출력, 자막 경계 및 겹침 검증.
- `.github/workflows/media-worker.yml`: 수동 실행 작업. 기본 `smoke`는 자체 생성된 1초 영상으로 FFmpeg 및 결과 패키징을 확인한다. **음성 인식이나 사이트 다운로드 성공을 증명하는 테스트가 아니다.**
- `download`, `transcribe-public-video` 작업은 입력한 공개 영상 URL을 사용한다. 사용자에게 다운로드 권한이 있는 자료만 입력한다. 로그인/DRM 우회 기능은 없다.
- 취소는 GitHub Actions의 Cancel workflow로 실행 프로세스를 중단한다. MISO UI의 취소 API 연결은 아직 없다.
- 결과는 1일 보관하는 Actions artifact다. 이 공개 저장소의 작업에는 공개해도 되는 자료만 사용한다.

## 현재 미완료

**MISO에서 실행 버튼을 누르는 것만으로 이 저장소가 호출되지는 않는다.** 다음 연결이 남아 있다.

1. MISO 서버 비밀 설정에 이 저장소만 대상으로 하는 실행용 Fine-grained PAT 등록. 실행에 필요한 권한은 Actions read/write. 코드 수정 권한과 실행 권한은 별개다.
2. MISO의 인증된 작업 생성/조회/취소/결과 다운로드 API와 GitHub workflow dispatch/run/artifact API 연결. GitHub 키는 브라우저로 내리지 않는다.
3. 미공개 영상·음성을 위한 보호된 파일 전송과 결과 경로 구현. 현재 공개 Actions 입력/출력으로 내부 자료를 처리하지 않는다.
4. 실제 공개 영상 다운로드와 한국어 음성 인식 검증, 사용자 MISO 화면에서 전체 흐름 검증.

CPU 기본 `large-v3`는 속도보다 정확도를 우선한 시작점이며, 모든 한국어 데이터에서 가장 정확하다고 주장하지 않는다. GPU 환경에서는 `YM_ASR_DEVICE=cuda`를 사용한다. 프로그램명·인명은 최종 검토가 필요하다. 20분·250 MB 제한이 있으며 대형 실파일 성능은 별도로 검증해야 한다.

로컬 실행: Python 3.12, FFmpeg/ffprobe, Node 22, `pip install -r requirements.txt`.

```
python -m unittest -v test_utility_worker
python utility_worker.py smoke
python utility_worker.py transcribe --input video.mp4 --language ko --output transcript-output
```

출력 폴더가 이미 있으면 덮어쓰지 않고 중단한다. `manifest.json`은 결과 크기와 SHA-256을 포함한다. 일반 로그에는 소스 URL·인식 문구를 기록하지 않는다. 실제 처리 결과와 `smokeOnly`를 구분한다.

공식 소스: https://github.com/yt-dlp/yt-dlp (Unlicense), https://github.com/SYSTRAN/faster-whisper (MIT), https://github.com/openai/whisper (MIT). FFmpeg 배포판의 개별 빌드 라이선스를 따른다.

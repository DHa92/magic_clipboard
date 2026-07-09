# magic_clipboard

Windows용 클립보드 히스토리 관리 프로그램. PySide6(Qt) 기반으로 한글 IME 입력이 자연스럽고,
텍스트/이미지를 자동 수집해서 2단계 카테고리와 키값으로 정리·검색할 수 있습니다.

## 기능

- **자동 수집** — 클립보드의 텍스트/이미지 변경을 이벤트로 감지해 SQLite에 저장 (연속 중복 제외)
- **이미지 지원** — 목록에 썸네일 표시, 복사하면 다시 이미지로 클립보드에 들어감
- **2단계 카테고리 + 키값** — 항목(다중 선택 가능)에 대분류/소분류/키값 지정
- **검색**
  - 일반 검색어: 내용 + 카테고리 + 키값 전체 매칭
  - `/k 검색어`: 키값만 검색
  - `/c 검색어`: 카테고리만 검색
  - 한글 조합 중인 글자(preedit)까지 실시간으로 검색에 반영
- **전역 단축키** (옵션에서 변경 가능)
  - `Alt+V`: 미니 UI 팝업 — 텍스트 캐럿 옆(없으면 마우스 옆)에 표시. Enter/더블클릭으로 복사하고 닫힘
  - `Alt+C`: 활성 프로그램의 선택 텍스트를 복사시켜 즉시 수집
- **트레이 상주** — 창을 닫아도 트레이에서 계속 수집. 트레이 클릭 = 전체 UI
- **옵션** — 로그인 시 자동 시작(레지스트리 Run 등록), 단축키 변경
- **중복 실행 방지** — 이미 실행 중이면 기존 창을 앞으로 가져옴

## 실행

### 파이썬으로 실행

```
pip install PySide6
python clipboard_manager.py        # 콘솔 없이 실행하려면 pythonw 사용
```

### 포터블 exe

`dist\magic_clipboard.exe` 하나만 복사해서 아무 폴더에서나 실행하면 됩니다.
데이터(`clipboard.db`)는 exe 와 같은 폴더에 저장됩니다.

직접 빌드하려면:

```
pip install pyinstaller PySide6
pyinstaller --noconfirm --onefile --windowed --name magic_clipboard --icon app.ico clipboard_manager.py
```

## 사용법 요약

| 동작 | 방법 |
|---|---|
| 미니 UI 열기 | `Alt+V` (전역) |
| 선택 텍스트 바로 수집 | 다른 앱에서 텍스트 선택 후 `Alt+C` |
| 항목 복사 | 더블클릭, Enter(미니), [복사] 버튼 |
| 카테고리/키 지정 | 항목 선택 → [카테고리/키 지정] |
| 삭제 | 항목 선택 → Delete 키 또는 [삭제] |
| 키값으로 검색 | 검색창에 `/k 키값` |
| 카테고리로 검색 | 검색창에 `/c 카테고리명` |
| 종료 | 트레이 아이콘 우클릭 → 종료 |

## 파일 구성

| 파일 | 설명 |
|---|---|
| `clipboard_manager.py` | 본체 (PySide6 단일 파일) |
| `clipboard.db` | 데이터 + 설정 (SQLite, 자동 생성, 커밋 제외) |
| `clipboard_manager_tk_backup.py` | 이전 tkinter 버전 백업 (참고용) |
| `Ditto/` | Ditto(C++) 소스에 카테고리 기능을 추가했던 실험 (별도 git 저장소, 커밋 제외) |

## 데이터 구조

SQLite `clips` 테이블: `id, kind(text/image), text, image(PNG BLOB), hash, category1, category2, item_key, created`
설정은 같은 DB의 `settings` 테이블(단축키 등)에 저장됩니다.

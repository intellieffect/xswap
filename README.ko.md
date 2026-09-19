[English](README.md) | **한국어**

# xswap

Codex CLI·macOS 데스크톱·로컬 OpenClaw에서 사용할 OpenAI 계정을 선택하는 오픈소스 도구입니다. 각 사용자가 **자신의 계정으로 로그인**합니다. 계정이나 구독을 팀원끼리 공유하는 도구가 아닙니다.

## 설치

필수: Python 3.11+, `uv`, 설치된 Codex CLI. OpenClaw 연결 시에는 로컬 `openclaw`와 `node`도 PATH에 있어야 합니다.

```sh
uv tool install 'git+https://github.com/intellieffect/xswap.git@v0.8.2'
xswap --version
```

명령을 찾지 못하면 `uv tool update-shell` 실행 후 새 터미널을 여십시오. `codex-swap`도 동일한 명령입니다. Claude 전용 `cswap`은 변경하지 않습니다.

## 30초 둘러보기

```sh
codex login                # 아직 로그인하지 않았다면 먼저 실행
xswap init                  # 로그인 등록, 계정 추가, 자동 전환·정책 설정, doctor까지 진행
xswap list                 # 등록된 모든 계정의 실시간 잔여량
xswap use --best            # 잔여 사용량이 가장 많은 계정을 기본값으로 선택
xswap                       # 선택한 계정으로 Codex CLI 실행
xswap openclaw --pool main,work   # OpenClaw가 두 계정을 스스로 순환하도록 등록
xswap doctor                # 읽기 전용 상태 점검 — 뭔가 이상하면 먼저 이것부터
```

## 무엇을 할 수 있나

- **직접 관리하는 계정을 등록**하고, 모델 프롬프트를 소모하지 않고 잔여 사용량을 확인 — [처음 사용하기](#처음-사용하기) 참고.
- **여유가 가장 많은 계정으로 실행**, 1회성 또는 이후 기본값으로 — [잔여 사용량 보기](#잔여-사용량-보기) 참고.
- **잔여량이 바닥나기 전에 알림** 받기 — [잔여량 알림](#잔여량-알림) 참고.
- **실행 중인 Codex 서버·대화를 유지한 채 계정만 전환** — [대화 중 자동 계정 전환](#대화-중-자동-계정-전환-실험적-앱--대화형-cli) 참고.
- **로컬 OpenClaw Gateway에 ChatGPT 인증을 동기화**, 필요하면 스스로 순환하는 풀로 — [OpenClaw도 함께 전환하기](#openclaw도-함께-전환하기) 참고.
- **문제가 생기면 명령 하나로 진단** — [오류가 나면](#오류가-나면) 참고.

## 목차

- [설치](#설치)
- [셸 자동완성](#셸-자동완성)
- [처음 사용하기](#처음-사용하기)
- [잔여 사용량 보기](#잔여-사용량-보기)
- [대화 중 자동 계정 전환](#대화-중-자동-계정-전환-실험적-앱--대화형-cli)
- [OpenClaw도 함께 전환하기](#openclaw도-함께-전환하기)
- [자주 쓰는 명령](#자주-쓰는-명령)
- [지원 범위와 동작 원리](#지원-범위와-동작-원리)
- [오류가 나면](#오류가-나면)
- [개발 및 검증](#개발-및-검증)
- [주간 대시보드와 메뉴바](#주간-대시보드와-메뉴바)

## 셸 자동완성

zsh는 `xswap completion zsh > "${fpath[1]}/_xswap"` 실행 또는 `.zshrc`에 `eval "$(xswap completion zsh)"` 추가, bash는 `.bashrc`에 `eval "$(xswap completion bash)"` 추가로 설정합니다. 서브커맨드·옵션·계정 이름(`xswap list --offline`으로 로컬에서만 조회, 네트워크 없음) 모두 자동완성됩니다.

## 처음 사용하기

```sh
codex login   # 아직 로그인하지 않았다면 먼저 실행
xswap init    # 그 로그인을 등록하고, 계정을 추가로 물어보고, 자동 전환·정책을 설정한 뒤 doctor까지 실행
```

<details>
<summary>xswap init이 실제로 하는 일</summary>

`xswap init`은 아래 네 단계를 대신 실행하는 얇은 마법사입니다: 현재 `codex login`이 아직 등록되지 않았다면 `main`으로 등록하고, 계정을 추가로 등록할지 물어보고(빈 값이면 건너뜀), 계정이 2개 이상이면 `auto-enable --wrap-codex`를 제안하고, `auto-policy --weekly-remaining 10`을 제안한 뒤 마지막으로 `xswap doctor`를 실행합니다. 각 단계는 건너뛸 수 있고 다시 실행해도 안전합니다. `--yes`를 주면 아무것도 묻지 않고 안전한 기본값만 적용하며(등록 대상이 있으면 `main`으로 등록, 계정 추가는 생략, 자동 전환은 켜지 않음), `--no-auto`는 자동 전환 단계를 건너뛰고, `--weekly-remaining PCT`는 정책 기본값을 덮어씁니다.

</details>

또는 네 단계를 직접 실행할 수도 있습니다.

```sh
xswap register main       # 현재 Codex 로그인 등록
xswap add work --use      # 브라우저에서 추가 OpenAI 계정으로 로그인하고 바로 선택
xswap login work          # work 계정의 로그인이 만료됐을 때 재인증
xswap use --best          # 잔여 사용량이 가장 많은 계정을 기본값으로 선택
xswap                     # 선택한 계정으로 Codex CLI 실행
xswap app                 # 선택한 계정의 별도 데스크톱 창 열기
```

현재 Codex에 로그인하지 않았다면 먼저 `codex login`을 실행하십시오. `xswap list`로 등록 계정을, `xswap status`로 선택된 계정의 로컬 로그인 상태를 확인할 수 있습니다. **처음 추가하는 계정은 `--use` 없이도 자동으로 선택됩니다.** 이후 `xswap add work`처럼 계정을 추가로 등록할 때는 기존 선택이 그대로 유지됩니다.

## 잔여 사용량 보기

```sh
xswap list                 # 실시간 잔여 비율·초기화 시각 (Spark 기본 숨김)
xswap list --include-spark # Spark 잔여량도 표시
xswap usage                # 선택한 계정만 조회
xswap usage work            # 지정 계정만 조회
xswap list --json           # 자동화용 조회 결과
xswap list --offline        # 네트워크 조회 없이 계정 목록만
```

각 한도에 `77% left`처럼 **남은 비율**을 표시하고, 초기화 시각(로컬 시간대)과 남은 시간을 함께 보여줍니다. Codex 기본 한도를 먼저, 모델별 추가 한도를 별도 줄에 표시합니다. `5h`·`7d` 등의 주기는 서버가 제공한 실제 기간이며, 없는 한도를 임의로 만들어 표시하지 않습니다. `credits`는 서버가 제공한 별도 크레딧 잔액이며 구독 잔여 비율과 다릅니다.

<details>
<summary>조회 방식(App Server, 캐시하지 않음)</summary>

조회는 공식 Codex App Server의 `account/rateLimits/read`를 사용합니다. 모델 대화나 테스트 턴을 생성하지 않으며, 기본 계정을 전환하지 않습니다. Codex가 필요에 따라 정상 인증 갱신을 수행할 수 있습니다. 계정당 최대 12초를 기다리고 최대 4개 계정을 병렬 조회합니다. 조회 실패·미로그인·API 키 계정은 구분하며, 알 수 없는 값을 `100%`로 표시하지 않습니다. 기본값은 매번 실시간 조회이며 캐시하지 않습니다(아래 「캐시된 조회」 참고).

</details>

### 캐시된 조회

<details>
<summary>캐시 파일과 무효화, 거부된 로그인 기록</summary>

`xswap list`·`xswap usage`·`xswap run --best`·`xswap use --best`는 매번 계정당 `codex app-server`를 새로 띄웁니다. 상태표시줄이나 크론처럼 자주 조회하는 호출자를 위해 `--cached SECONDS`를 붙이면 그만큼 신선한 기존 결과를 재사용하고 새로 띄우지 않습니다 — `--cached`를 주지 않으면 기존과 동일하게 항상 실시간 조회입니다. 조회가 성공하면 결과는 항상 `~/.local/share/codex-swap/usage-cache.json`(권한 `0600`)에 계정 이름별로 저장되며, 화면에 보이는 것과 동일한 화이트리스트 필드 — 잔여 비율·초기화 시각·플랜 종류·크레딧, 그리고 이메일일 수도 있는 로컬 계정 라벨 — 만 담고 원본 서버 응답이나 토큰은 담지 않습니다. 저장된 라벨이 현재 로그인 라벨과 다르면(같은 이름으로 재로그인한 경우) 캐시를 재사용하지 않고 새로 조회합니다. `--offline`과 `--cached`는 동시에 쓸 수 없고, `run`·`use`에서는 `--best`와 함께여야 합니다.

사용량 서비스가 로그인을 거부한 경우(조회에 401 계열 응답, 실시간 목록에는 `sign-in required · xswap login 이름`으로 표시)는 `~/.local/share/codex-swap/auth-state.json`(권한 `0600`; 계정 이름·로컬 로그인 라벨·시각·짧은 사유만 담고 토큰은 담지 않음)에도 기록됩니다. 이 기록이 남아 있는 동안 `xswap list`/`usage`의 `--cached`·`--offline`은 Codex를 띄우지 않고 `sign-in required · xswap login 이름`을 표시하고, `run --best`/`use --best`와 자동 전환 풀은 해당 계정을 조회 없이 건너뛰며, `xswap use 이름`은 거부하고, `xswap doctor`가 보고합니다. `--cached` 없는 실시간 `xswap list`/`usage`는 다시 조회하며 성공하면 기록을 지웁니다. `xswap login 이름`도 기록을 지웁니다. 사용량 캐시와 같이, 저장된 라벨이 현재 로그인 라벨과 일치할 때만 유효합니다.

</details>

### 잔여량 알림

```sh
xswap list --warn 15   # codex 한도가 15% 미만 남은 창이 하나라도 있으면 경고
```

`--warn PCT`는 1~100 사이 값만 받습니다. 기존 표(또는 `--json`) 출력은 그대로 stdout에 찍히고, 그 뒤 비활성화되지 않은(disabled 아닌) 계정 중 조회에 성공한 계정의 `codex` 한도 창을 검사해 기준치 미만인 창마다 `warn: 이름 창 N% left (resets ...)` 한 줄을 stderr로 출력합니다. 알 수 없는 잔여값은 절대 경고를 발생시키지 않습니다. 경고가 하나라도 발생하면 `xswap list --warn`은 종료코드 `3`을 반환하고, 아니면 `0`을 반환합니다. `--warn` 없는 평범한 `xswap list`는 영향받지 않고 항상 `0`을 반환합니다.

대화형으로 쓰기보다 `launchd`나 `cron`에서 주기적으로 호출하는 용도입니다. `xswap alert --install`이면 그 등록까지 대신 해줍니다.

```sh
xswap alert --install --warn 15 --every 30
```

<details>
<summary>xswap alert --install이 만드는 파일</summary>

macOS에서는 `~/.local/share/codex-swap/alert/run.sh`(설치 시점에 확정한 절대경로의 `xswap`을 호출하는 래퍼 — `launchd`의 `PATH`에는 `/opt/homebrew/bin`이 없기 때문이며, `warn:` 줄마다 `osascript` 알림으로 바꿔줍니다)와 `~/Library/LaunchAgents/com.intellieffect.xswap.alert.plist`를 만든 뒤 `launchctl bootstrap`으로 등록합니다. 매 실행 결과는 `alert/last.log`에 남습니다. plist의 `EnvironmentVariables`에는 이 설치가 대상으로 삼은 상태 루트(`CODEX_SWAP_HOME`)가 함께 들어갑니다. launchd는 잡에 자체 환경을 주기 때문에, 이것이 없으면 잡은 *기본* 루트를 읽어 설치한 루트가 아닌 다른 루트의 풀·예약분·선택을 보고, `--auto-switch`를 켠 경우 매 주기마다 그 다른 루트의 선택을 바꿔놓았습니다. plist는 `0600`으로, xswap이 만드는 경우 `~/Library/LaunchAgents`는 `0700`으로 생성하며, 재설치는 실행 중일 수 있는 `run.sh`를 덮어쓰지 않고 새 파일을 만들어 이름만 바꿔 끼웁니다. 상태 확인은 `xswap alert --status`, 제거는 `xswap alert --uninstall`입니다. macOS가 아니면 `--install`은 아무것도 쓰지 않고 대신 동등한 `cron` 한 줄만 출력하며, 그 줄도 같은 상태 루트를 먼저 export합니다.

</details>

#### 세션 사이의 선제 전환

세션 안에서는 브리지가 턴 시작 시점에만 판단하고, 세션 밖에서는 아무것도 기본 선택을 옮기지 않습니다. 그래서 선택된 계정의 주간 한도가 바닥난 뒤 새로 여는 세션이나 `codex exec`는 소진된 계정으로 시작됩니다. `xswap auto-tick`이 세션 사이의 점검 명령이며, 알림과 같은 `launchd`/`cron` 주기에서 돌리는 용도입니다.

```sh
xswap auto-tick --cached 600          # 1회 점검. 종료코드 0 전환함 / 1 오류 / 2 조치 없음 / 3 차단됨
xswap auto-tick --dry-run             # 판단만 출력하고 선택·전달은 하지 않음
xswap alert --install --auto-switch   # 알림 잡에서 warn 단계 전에 실행
```

<details>
<summary>auto-tick 판단 규칙</summary>

자동 전환 설정(`xswap auto-enable`, `xswap auto-policy --weekly-remaining PCT`)의 풀과 주간 예비율을 읽고 `xswap list`와 같은 방식으로 사용량을 조회한 뒤(`xswap list`와 같이 래핑된 `codex` 항목을 필요하면 복구합니다), 브리지가 턴 전에 적용하는 규칙을 그대로 적용합니다. `xswap use`로 선택된 계정이 예비율 이하이거나 한도에 도달했거나 재로그인이 필요하면, 예비율을 **넘는** 풀 계정 중 여유가 가장 큰 계정을 `xswap use NAME`과 같은 경로로 선택합니다 — 새 세션의 기본값이 바뀌고, 풀 순서에서 그 계정이 맨 앞으로 오며, 실행 중인 브리지에는 수동 전환 요청이 전달됩니다(유휴 세션은 즉시, 작업 중인 세션은 턴이 끝난 뒤). 비활성화(disabled)·재로그인 필요 계정은 대상이 되지 않습니다. 잔여량을 알 수 없으면 전환하지 않고(`no-action:`, 종료코드 `2`), 자동 전환이 꺼져 있거나 선택된 계정이 없거나 예비율을 넘는 풀 계정이 없으면 `blocked:`와 함께 `3`으로 끝납니다. 조회 도중 수동으로 `xswap use`를 실행했다면 그 선택이 우선합니다. 규칙이 선택된 계정과 예비율만 비교하고 주간 잔여량은 초기화 때만 늘어나므로 별도의 쿨다운 없이도 전환이 되돌아가며 흔들리지 않습니다. 디렉터리 매핑은 보지 않습니다.

`--auto-switch`로 설치하면 `run.sh`가 `list --warn` 전에 `xswap auto-tick --cached SECONDS`를 실행하고(표와 경고는 전환 후 상태를 같은 캐시로 보여줍니다) `switched:` 줄을 "xswap auto-switch" 제목의 알림으로 바꿉니다. `xswap alert --status`에 `auto-switch: on`으로 표시되며, 플래그 없이 다시 `--install`하면 그 단계가 빠집니다. 자동 전환이 켜져 있어야 실제로 동작하며, 꺼져 있으면 `--install`이 그렇게 알려줍니다.

</details>

### 상태 표시줄(status line)

`xswap list --short`는 한 줄만 출력합니다. 계정마다 `{*}{name} {p5h}/{p7d}` 형식으로 ` · `로 이어 붙이며, `*`는 활성 계정 표시, `p5h`/`p7d`는 Codex 한도의 1차/2차 윈도우 잔여 비율(알 수 없으면 `?`)입니다. 비활성화(disabled)된 계정은 `list --short`에서 제외되지만, `xswap usage <계정명> --short`는 비활성화된 계정이라도 지정한 계정을 항상 표시하며, 계정이 하나도 없으면 `list --short`는 빈 줄을 출력합니다. `--short`는 `--offline`과 함께 쓸 수 있고, `--json`과는 동시에 쓸 수 없습니다. tmux 상태 표시줄 예시:

```sh
set -g status-right '#(xswap list --short)'
```

기존 버전 업데이트:

```sh
xswap upgrade                    # 최신 태그로 재설치, 특정 버전은 --tag vX.Y.Z
xswap upgrade --dry-run          # 실행 없이 명령만 출력
```

또는 아래 명령을 직접 실행:

```sh
uv tool install --force 'git+https://github.com/intellieffect/xswap.git@v0.8.2'
```

## 대화 중 자동 계정 전환 (실험적, 앱 + 대화형 CLI)

```sh
# 일회성 실행
xswap app --auto --accounts work,main
xswap run --auto --accounts work,main

# 기본 실행에도 적용하고 codex 명령 연결 (원복 정보 보관)
xswap auto-enable --accounts work,main --wrap-codex
codex                       # 자동 전환 대화형 CLI
codex resume --last          # 자동 모드 CLI 대화 이어가기
xswap app                   # 자동 전환 앱 창
xswap auto-status
xswap auto-disable          # 기본 실행 및 codex 심볼릭 링크 원복
```

<details>
<summary>--accounts와 codex 연결 시 환경변수 처리</summary>

`--accounts`는 본인이 로그인한 계정 이름을 우선순위 순서로 지정합니다. 최소 두 개가 필요합니다. 자동 기본 설정을 켜기 전에는 기존 실행 동작을 유지합니다. `xswap run --account 이름`과 `xswap app 이름`은 고정 계정으로 실행합니다. CLI의 대화형 실행·resume·fork·agents를 지원합니다. `codex exec`, `review` 등 `--remote` 훅이 없는 비대화형 명령은 실행 중 전환되지는 않지만, `codex` 명령이 연결된 뒤에는 `xswap run -- exec …`와 같은 규칙(현재 디렉터리 매핑, 없으면 `xswap use`로 선택한 계정)으로 선택 계정의 `CODEX_HOME`을 넣고 셸의 `OPENAI_API_KEY`/`CODEX_API_KEY`/`CODEX_ACCESS_TOKEN`을 제거한 채 한 번 실행되며, stderr에 `xswap: running codex exec as work` 한 줄을 남깁니다. xswap이 시작하는 모든 실행은 같은 목록에 더해 Codex CLI가 실제로 읽는 변수들도 제거합니다. `CODEX_APP_SERVER_CHATGPT_BASE_URL`, `OPENAI_BASE_URL`, `CODEX_REFRESH_TOKEN_URL_OVERRIDE`, `CODEX_REVOKE_TOKEN_URL_OVERRIDE`, `CODEX_CA_CERTIFICATE`, `CODEX_SQLITE_HOME`과 워크로드 아이덴티티·페더레이션 묶음(`CODEX_WORKLOAD_IDENTITY_*`, `OPENAI_WORKLOAD_IDENTITY_*`, `OPENAI_IDENTITY_TOKEN_FILE`, `OPENAI_FEDERATION_RULE_ID`)입니다. 그대로 두면 호출한 셸에서 export한 값이 xswap이 방금 선택한 계정의 리프레시 토큰을 어디로 보낼지, 사용량을 어느 백엔드가 답할지, 그 세션이 무엇으로 인증될지를 대신 정해 버립니다(`XSWAP_QUIET=1`은 이 줄만 숨깁니다). `CODEX_HOME`이 이미 설정됐거나 `XSWAP_BYPASS=1`인 경우, `--help`/`--version`/`--remote` 형식, 그리고 `login`, `logout`, `app`, `app-server`, `completion`, `help`, `update`, `upgrade`는 알림 없이 원래 홈으로 전달됩니다(`codex update`·`codex upgrade`가 계정 프로필 안에 릴리스를 설치하면 안 되기 때문입니다). `xswap run -- upgrade`도 자동 풀을 쓰지 않고 선택된 계정의 홈으로 실행되며, 릴리스는 `packages` 링크를 통해 기준 Codex 홈에 설치됩니다. 선택 계정이 비활성·미로그인 등으로 쓸 수 없으면 원래 홈으로 실행하고 이유와 조치를 한 줄 경고로 출력합니다. 잔여량 기준으로 고르려면 `xswap run --best -- exec …`를 사용하십시오. 0.8.0 이전에 저장된 `codex exec` 세션은 `~/.codex/sessions`에 남아 있으며 `XSWAP_BYPASS=1 codex exec resume …`로 이어갈 수 있습니다. `XSWAP_BYPASS=1`은 한 명령 앞에 붙이는 용도입니다. export하면 그 셸의 모든 `codex`에서 연결이 꺼지므로 이제는 조용히 넘어가지 않고 보고합니다. `xswap doctor`는 `codex bypass` 행을 추가하고(래퍼 레코드가 켜져 있으면 FAIL, 자동 전환이 꺼져 있어 아직 효력이 없으면 WARN), `xswap auto-status`는 `codexWrapped: false`와 `wrapperReason: bypass-variable`을 보고하며, `xswap use`/`switch`는 선택이 xswap과 `xswap app`에만 적용된다고 알립니다. 또한 `OPENAI_API_KEY`처럼 xswap이 시작하는 세션의 환경에서는 제거되므로, `xswap run`을 실행한 셸에서 export한 우회 설정이 그 명령이 방금 구성한 세션 안에서 래퍼를 끄지 못합니다.

</details>

자동 모드 앱과 CLI는 각각 **실행 중인 한 개의 Codex 서버와 대화 저장소를 유지**합니다. CLI는 원본 TUI 프로세스도 그대로 유지하며 사용자 전용 Unix WebSocket으로 서버와 연결합니다. TCP 포트를 열지 않습니다. 앱과 CLI 저장소는 분리되어 있습니다. 새 턴 전에 현재 사용량을 확인하고, 적용되는 한도가 소진되면 잔여량이 확인된 다음 계정으로 인증을 바꿉니다. 한도 정보는 최대 30초 동안 재사용하며, Codex의 한도 알림으로 갱신합니다. 모델별 한도를 확인할 수 없는 후보는 선택하지 않습니다.

진행 중인 턴이 `usageLimitExceeded`로 종료되면 기존 실패 알림을 보존하고 같은 대화에 이어가기 턴을 추가합니다. 원래 사용자 요청을 다시 전송하거나 도구 호출을 재생하지 않습니다. 기존 도구 결과를 확인하며 계속하도록 지시하지만, 모델이 수행하는 외부 작업 자체의 exactly-once 실행을 보장하는 것은 아닙니다. 관측된 다른 턴이 실행 중이면 모두 종료될 때까지 인증 전환을 미룹니다. 사용자가 중단하거나 새 요청을 보내면 대기 중인 자동 이어가기를 취소합니다.

사용 가능한 후보가 없으면 원래 한도 오류를 남기고 멈춥니다. 같은 이어가기 체인에서 동일 계정을 반복 사용하지 않으며, 일반 429/연결 오류/권한 오류/컨텍스트 초과를 한도 소진으로 간주하지 않습니다. 다음 사용자 요청에서는 사용량을 다시 확인할 수 있습니다.

**처음 한 번은 자동 모드로 앱/CLI 세션을 시작해야 합니다.** 이미 실행 중인 일반 창이나 CLI 프로세스에 연결을 끼워 넣지 않습니다. 자동 모드에서 시작한 대화는 이후 계정 전환에도 유지되지만, 기존 계정별 창의 대화는 자동 복사하지 않습니다. 모든 풀 계정은 이 창의 대화·파일 작업 문맥을 공유하므로 개인 계정과 다른 조직의 계정을 섞지 마십시오.

<details>
<summary>인증 재확인(verifiedAccount)과 실행 기록</summary>

인증은 원래 각 계정의 `auth.json`에서 메모리로 읽고, 만료 시 원래 계정의 공식 CLI를 통해 갱신합니다. 자동 모드 홈에 인증 파일을 복사하지 않으며, 기존 계정 선택·OpenClaw·이미 실행 중인 일반 CLI 세션을 변경하지 않습니다. 토큰 갱신 요청은 같은 계정으로만 처리합니다. `auto/status.json`과 `auto/cli-runs/*/status.json`에는 선택 계정, PID, 전환 횟수, 마지막 상태만 저장하며 토큰·대화 내용은 저장하지 않습니다. 다시 읽을 일이 없는 `auto/cli-runs/` 기록은 `xswap auto-status`, `xswap list`/`usage`의 텍스트 출력(`alert` 작업이 주기마다 정리), 메뉴 막대의 `dashboard` 갱신, 새 브리지 CLI 실행마다 부수 효과로 정리합니다. 브리지가 정상 종료(`reason` 없는 `stopped`)한 기록은 24시간 뒤, 실패 `reason`이 있는 `stopped` 기록이나 `stopped` 기록 없이 브리지가 사라진 기록(강제 종료·크래시·재부팅)은 비정상 종료를 확인할 수 있도록 7일 뒤, 잠금 파일 외에 아무것도 없는 디렉터리(브리지 연결 전에 TUI가 종료된 경우)는 1분 뒤에 제거합니다. `auto-status --prune`은 실행 중이 아닌 기록을 즉시 전부 정리합니다. 어느 경우에도 실행 중인 브리지(잠금 보유)와 생성된 지 60초 미만인 기록은 브리지가 아직 잠금을 잡기 전일 수 있으므로 절대 정리하지 않습니다. `auto-status`는 제거한 기록을 `prunedRuns`(실행 ID·규칙·경과 시간·계정·마지막 이벤트·브리지 버전·종료 사유)로 보고하며, `list --json`/`--short`, `doctor`, `upgrade`는 아무것도 제거하지 않습니다.

각 실행 디렉터리에는 `bridge.log`(0600)도 남습니다. 브리지 이벤트마다 한 줄(로컬 시각, 이벤트, 계정, 요청·후보 계정, 짧은 원인, 실패 시 예외 종류)을 기록하며 토큰·프롬프트·원본 오류는 절대 담지 않고, 1 MiB를 넘으면 오래된 줄부터 버립니다. 실패 원인은 `usage service unavailable`, `ChatGPT access token needs refresh or sign-in`, `selected account is disabled or removed`, `Codex rejected account/login/start`, `app-server request timed out` 같은 짧은 문구로 분류되어 `status.json`의 `reason`·`manualReason`(수동 전환이 대기·실패한 이유, 다음 요청 전까지 유지)·`lastFailure`(세션의 마지막 실패 이벤트)에 기록되고, `xswap auto-status`는 세션별 `log` 경로도 보여줍니다. `xswap use`/`switch`/`login`은 브리지가 요청을 거부한 이유를 `Failed: …`로 출력합니다.

브리지는 앱 서버에 로그인을 보낼 때마다(세션 시작, 자동 전환, `xswap switch`, `xswap login` 재로그인) `account/read`로 서버가 실제로 들고 있는 로그인을 다시 읽어 `verifiedAccount`·`verifiedIdentity`·`verifiedAt`으로 기록합니다(`account`는 브리지가 보냈다고 믿는 계정일 뿐입니다). 서버가 로그인을 갖고 있지 않거나 다른 로그인을 들고 있으면 전환을 `identity mismatch` 사유로 실패 처리하고 이전 계정의 로그인을 다시 보내 세션을 이전 계정에 유지하며, 세션 시작 시점이면 알 수 없는 계정으로 열지 않고 브리지를 중단합니다. 서버가 답할 수 없으면(`account/read`가 없는 Codex CLI, 시간 초과, 이메일 클레임이 없는 토큰) 전환은 유지하고 `verifyReason`에 이유를 남깁니다. 플랜 종류는 신원이 아닙니다. `xswap doctor`의 `auto sessions` 항목이 확인되지 않은 실행 세션과, 확인 이후 다른 사용자로 로그인이 바뀐 계정을 경고합니다.

</details>

`codex` 명령이 아직 연결되지 않았으면 `xswap use`가 일반 `codex`가 계속 쓰는 홈과 계정을 출력하고, 계정이 2개 이상이면 `xswap doctor`가 경고합니다.

`codex` 래핑·PATH 해석 내부 동작과 실제 Codex 재배치는 [docs/automatic-switching-internals.ko.md](docs/automatic-switching-internals.ko.md)에 옮겼습니다.

실제 Codex CLI 0.153.4와 macOS 앱 내장 바이너리에 가짜 계정/로컬 HTTP 서버를 연결하여 검증했습니다. `첫 계정 응답 → 한도 오류 → 두 번째 계정 응답`에서 서버 PID·대화 ID가 유지되고 3개 턴이 보존됐습니다. 실제 TUI를 PTY로 실행한 검사에서도 TUI/서버 PID와 대화 ID가 유지되며 자동 전환 후 다음 사용자 요청을 처리하고 Ctrl-C로 정상 종료됐습니다. 실제 구독 한도를 소진시키는 테스트는 하지 않습니다.

## OpenClaw도 함께 전환하기

```sh
xswap openclaw work --dry-run       # 적용할 계정·에이전트 확인
xswap openclaw work                 # 로컬 OpenClaw 전체 에이전트에 적용
xswap use main --openclaw           # CLI·앱 기본 계정과 OpenClaw를 함께 전환
xswap openclaw --pool main,work     # OpenClaw가 두 계정을 스스로 순환하도록 등록
```

`xswap openclaw`는 이름을 생략하면 현재 선택 계정을 사용합니다. 특정 에이전트만 바꾸려면 `xswap openclaw work --agent main --agent devagent`처럼 지정하십시오. 이 명령 자체는 xswap의 기본 계정을 바꾸지 않습니다.

<details>
<summary>풀 공유·조직 불일치 거부 규칙</summary>

`--pool a,b,c`는 등록된 이름 2개 이상을 쉼표로 지정하며, 위치 인자 NAME과는 함께 쓸 수 없습니다. 나열한 순서 그대로 각 에이전트의 OpenAI 인증 순서에 전부 등록하면, 이후 OpenClaw가 자신의 쿨다운 로직(`resolveAuthProfileOrder`·`isProfileInCooldown`·`markAuthProfileFailure`/`markAuthProfileCooldown`)으로 그 안에서 스스로 순환합니다. 즉 한 계정이 한도에 걸려도 사람이 `xswap openclaw other`를 실행할 때까지 기다릴 필요가 없습니다. 풀에 묶인 에이전트는 그 순간 선택된 계정이 무엇이든 동일한 대화·작업 맥락을 공유하므로, 서로 다른 ChatGPT 조직(계정)을 섞으면 그 조직들의 맥락이 한 대화 안에서 뒤섞입니다 — `sync_openclaw`는 풀에 속한 계정들의 조직이 갈리면 기본적으로 중단하며, 의도한 것이면 `--allow-mixed`로 넘길 수 있습니다. 계정의 조직 자체를 확인할 수 없는 경우(인증 파일을 읽을 수 없거나 해독 가능한 클레임이 없는 경우)도 실제로 조직이 다를 때와 동일하게 중단합니다 — 확인 불가를 일치로 간주하지 않습니다.

</details>

OpenClaw 연결은 다음을 수행합니다.

1. 선택 계정(들)의 ChatGPT OAuth 인증을 확인합니다. `--pool`이면 계정 간 조직 일치 여부도 함께 확인합니다.
2. 변경 대상 인증·우선순위만 작은 0600 파일로 백업합니다.
3. OpenClaw의 공개 SDK와 잠금·트랜잭션을 통해 인증을 등록하고 대상 에이전트의 OpenAI 인증 선택을 변경합니다.
4. `openclaw secrets reload`로 실행 중인 Gateway 인증 상태를 재적용합니다.

원래 인증 프로필은 보존합니다. 기존 모델·채널 설정이나 대화 기록은 변경하지 않으며, 테스트 메시지를 자동 전송하지도 않습니다. 정상 완료는 **저장·선택·Gateway 재적용 확인**을 뜻하며 모델의 실제 응답이나 잔여 사용량을 보장하지 않습니다.

백업 경로를 바꾸려면 `xswap openclaw work --backup-dir /path/to/private-backups`를 사용하십시오. 전체 미디어·대화 DB를 백업하지 않습니다.

### 죽은 OpenClaw 쿨다운

OpenClaw는 429 한 번이면 해당 시각 기준 한도 초기화 시점까지 OpenAI 인증 프로필을 잠그고, 그 창이 끝나기 전에는 다시 확인하지 않습니다. 실제 한도가 그보다 먼저 풀려도(플랜 업그레이드, 상위에서의 수동 초기화 등) 프로필은 그대로 잠겨 있습니다 — 증상은 `xswap list`에서 `openai usage: 100% left`인데 `openclaw models status`에는 `[cooldown 6d]`가 나란히 뜨는 것입니다. `xswap doctor`는 이를 `이름: openclaw cooldown` 항목으로 보고합니다: 쿨다운 중인데 xswap 자체 사용량 캐시에 실제 잔여량이 보이면 `FAIL`, 쿨다운 중이지만 잔여량을 로컬에서 알 수 없으면 `WARN`이며, 비활성화된 계정은 절대 실패로 표시하지 않습니다.

```sh
xswap openclaw 이름 --clear-cooldown --dry-run
xswap openclaw 이름 --clear-cooldown
xswap openclaw --pool a,b --clear-cooldown --yes
```

로컬 Gateway를 멈추고(`openclaw gateway stop --force`) 상태 DB를 비공개로 백업한 뒤, 지정한 계정(들)의 — NAME/`--pool`을 생략하면 등록된 모든 계정의 — 죽은 `blockedUntil`/`blockedReason`/`blockedSource` 키만 지우고 오류 횟수를 0으로 되돌린 다음 Gateway를 다시 시작합니다. Gateway 정지가 실패하면 아무것도 쓰지 않고 중단하며, `--dry-run`은 지울 항목만 보여줄 뿐 Gateway도 DB도 건드리지 않습니다.

## 자주 쓰는 명령

| 명령 | 동작 |
|---|---|
| `xswap list` | 계정 목록·잔여 사용량·초기화 시간 표시 |
| `xswap run --account main -- resume` | 지정 계정으로 Codex 명령 실행 |
| `xswap run --best -- exec "요약해줘"` | 잔여 사용량이 가장 많은 계정으로 실행(1회성 선택) |
| `xswap use --best` | 잔여 사용량이 가장 많은 계정을 이후 실행의 기본값으로 선택 |
| `xswap app work` | 지정 계정의 macOS 앱 실행 |
| `xswap add work --device-auth` | Codex의 기기 코드 로그인 사용 |
| `xswap app work --dry-run` | 앱 실행 경로와 환경변수 확인 |
| `xswap login work` | 등록된 계정을 재인증(로그인 만료 시) |
| `xswap disable work` | 계정을 삭제하지 않고 선택 대상에서 제외 |
| `xswap enable work` | 제외된 계정을 다시 선택 대상으로 복원 |
| `xswap remove work` | 계정을 xswap에서 제거 |
| `xswap map work ~/code/company` | 해당 디렉터리 하위에서는 기본으로 work 선택 |
| `xswap unmap ~/code/company` | 디렉터리 매핑 제거 |

`xswap disable`로 제외된 계정은 `use`, `--auto`/`auto-enable` 계정 풀, `openclaw`, `list`의 실시간 사용량 조회에서 모두 빠지며 목록에는 `(disabled)`로 표시됩니다. 다만 `xswap run --account NAME`·`xswap app NAME`처럼 계정을 명시적으로 지정한 단일 실행은 막지 않습니다.

`xswap remove`는 계정의 레지스트리 항목만 지우고 파일은 기본적으로 남겨두며, `--purge`를 추가하면 관리형 프로필 디렉터리까지 삭제합니다(등록된 홈, 즉 사용자의 `~/.codex`는 어떤 플래그를 줘도 삭제하지 않습니다). 활성화된 `--auto`/`auto-enable` 풀에 속한 계정, 그리고 실행 중인 자동 세션이 붙잡고 있는 계정은 거부합니다. 지금 그 세션이 쓰는 계정뿐 아니라 그 세션의 풀에 든 계정도 포함됩니다. 현재 계정이 주간 한도에 걸렸을 때 옮겨 갈 로그인이기 때문입니다. `--purge`는 레지스트리가 실제로 가리키는 관리형 프로필만 삭제합니다. 기록된 홈이 다른 곳이면(다른 `CODEX_SWAP_HOME` 아래로 복원·복사된 레지스트리, 손으로 고친 `home`) 삭제하지 않고 두 경로를 모두 밝히며 거부합니다. 그러지 않으면 레코드가 가리키지 않는 디렉터리를 지우면서 정작 제거한 로그인은 디스크에 남습니다. `--yes` 없이는 확인을 묻습니다.

## 지원 범위와 동작 원리

Codex 인증 저장은 `file` 방식만 지원합니다. `keyring`/`auto`는 변경 없이 오류로 중단합니다. CLI 프로필은 macOS/Linux에서 사용할 수 있으며 데스크톱 실행은 macOS 전용입니다. Windows는 지원하지 않습니다.

OpenClaw 연결은 **ChatGPT OAuth + 로컬 Gateway** 전용입니다. API 키 인증이나 원격 Gateway를 이 명령으로 교체하지 않습니다. OpenClaw 2026.8.1에서 실제 검증했으며, 공개 SDK가 없는 버전에서는 중단합니다. Gateway가 실행 중이어야 인증 재적용까지 완료됩니다.

<details>
<summary>xswap use의 적용 범위와 디렉터리 매핑 규칙</summary>

`xswap use`는 이후 **xswap으로 실행하는** CLI와 앱에 적용됩니다. 일반 `codex` 명령과 이미 열린 앱·CLI 세션에는 소급 적용되지 않습니다. `--openclaw`를 붙이지 않으면 OpenClaw는 변경하지 않습니다. 명시적으로 계정을 고정한 기존 OpenClaw 세션이나 진행 중인 작업은 기존 인증을 유지할 수 있습니다. 계정을 명시하지 않았을 때 정확히 다음 명령만 디렉터리 매핑을 따릅니다: 계정 없는 `xswap`·`--account` 없는 `xswap run`·`xswap app`·`xswap status`·`xswap usage`(가장 깊이 일치하는 매핑 우선, 매핑이 없을 때만 `xswap use`로 선택한 계정으로 돌아감. 우선순위: 명시적 계정 > 디렉터리 매핑 > 선택된 계정). `xswap openclaw`는 이름을 생략해도 디렉터리 매핑을 절대 따르지 않고 항상 `xswap use`로 선택한 계정을 사용합니다 — 하나의 실행 세션이 아니라 로컬 OpenClaw 에이전트 전체의 공유 인증 상태를 바꾸기 때문입니다. 비활성화(`disable`)된 계정에 매핑돼 있어도 해당 매핑은 그대로 적용되어 실행되며, 이는 `disable`이 `use`·`--auto`/`auto-enable` 풀·`openclaw`·실시간 사용량 조회에만 영향을 준다는 기존 규칙과 일치합니다.

</details>

현재 계정을 등록할 때는 기존 `CODEX_HOME`을 참조합니다. 추가 계정은 독립된 인증·대화·DB를 사용하며, `config.toml`, `AGENTS.md`, `skills`, `rules`는 원래 홈을 공유합니다. `plugins`는 각 홈에 독립된 코드 캐시를 생성하고 이후에는 해당 홈의 플러그인 관리자가 갱신합니다. 다른 홈의 플러그인 변경을 자동 덮어쓰지 않습니다. **계정 분리는 파일·도구 접근 권한을 격리하는 보안 샌드박스가 아닙니다.**

OpenClaw 연결은 실행 시점의 인증을 동기화합니다. 이미 OpenClaw에 더 최신 토큰이 있으면 이를 보존하고, 동일 만료 시각에 서로 다른 토큰이면 덮어쓰지 않고 중단합니다. Codex와 OpenClaw 사이에 상주 동기화 데몬을 설치하지 않습니다. 다른 도구에서 재로그인한 뒤에는 `xswap openclaw 이름`을 다시 실행하십시오.

데스크톱은 계정별 `CODEX_HOME`과 `CODEX_ELECTRON_USER_DATA_PATH`로 실행합니다. ChatGPT 26.901.20858의 별도 프로필 실행과 내장 Codex의 인증 인식을 검증했습니다. 이 환경변수는 공식 안정 API로 보장되지 않으므로 앱 업데이트 후 새 창의 프로필 메뉴에서 계정을 확인하십시오.

## 오류가 나면

<details>
<summary>xswap doctor가 점검하는 항목</summary>

먼저 `xswap doctor`를 실행하십시오. 네트워크 호출 없이 읽기 전용으로 codex 실행 파일, PATH의 모든 `codex`와 래퍼 연결 상태, 인증 저장 방식, 등록 계정별 로그인·토큰 만료·사용량 조회에서 거부된 로그인 기록, 플러그인 링크, 자동 전환 풀, 실제 Codex 실행 파일 위치(`real codex`, `packages link`), 설치된 xswap보다 낮은 브리지로 실행 중인 세션, 실행 중인 자동 세션의 서버 확인 로그인, OpenClaw plugin SDK를 점검합니다. Codex 업데이트가 다른 실행 파일로 되돌려 놓은 `codex` 항목은 FAIL로 보고하며, 다음 `xswap` 실행·`list`·`use`가 다시 연결할 때까지 `xswap doctor`는 종료 코드 1을 반환합니다. xswap이 래핑할 수 없는 항목(링크 대상이 없거나 실행 파일이 아님, 심볼릭 링크 자리의 일반 파일, 다른 사용자 소유 링크, 상대 경로 PATH 항목으로 찾은 `codex`, 자동 전환이 꺼진 경우)은 항목이 알려주는 조치를 직접 할 때까지 FAIL과 종료 코드 1이 유지됩니다. `codex 실행 파일` 행도 자신이 하는 조회에 같은 규칙을 적용합니다. 상대 경로 PATH 항목으로만 찾은 `codex`는 doctor를 실행한 디렉터리의 파일을 실행하고 이 머신의 Codex로 보고하는 대신, 그 항목을 명시하며 FAIL로 보고합니다. 두 행은 파일이 아니라 환경을 보고합니다: doctor를 실행한 셸에 `XSWAP_BYPASS=1`이 설정돼 있으면 `codex bypass`, `CODEX_SWAP_HOME`이 이 셸이 읽을 상태 루트를 고른 경우 `state root`입니다. `xswap doctor --json`은 자동화용 출력이며, 하나라도 실패(FAIL)하면 종료 코드 1을 반환합니다.

</details>

| 상황 | 다음 행동 |
|---|---|
| `saved ... Gateway reload failed` | 저장은 됐습니다. 로컬 Gateway를 확인한 뒤 `openclaw secrets reload`를 실행하십시오. `use --openclaw`의 기본 계정 선택은 아직 바뀌지 않았으므로 원래 명령을 다시 실행하십시오. |
| `sync incomplete (N/M agents)` | 일부 적용됐습니다. 오류에 표시된 백업을 보존하고 저장 공간·권한을 확인한 후 같은 명령을 다시 실행하십시오. |
| `expired` / `diverged` | 해당 Codex 계정을 열어 토큰을 갱신하거나 재로그인한 뒤 다시 실행하십시오. |
| `usage limit` | 인증 교체로 구독 사용량 한도가 늘어나지는 않습니다. 사용 가능한 다른 본인 계정이나 한도 초기화를 확인하십시오. |
| 설치 실패 | Python 3.11+, Git, uv 설치와 GitHub HTTPS 연결을 확인하십시오. |

## 개발 및 검증

```sh
git clone https://github.com/intellieffect/xswap.git
cd xswap
python3 -m unittest discover -v
node --test tests/*.test.mjs
python3 tests/live/live_codex_smoke.py  # 실제 Codex + 가짜 계정/로컬 서버 검증
uv tool install .
```

CI는 macOS/Linux, Python 3.11/3.14, Node 22에서 테스트와 설치를 확인합니다. 자동 테스트는 가짜 인증만 사용합니다. 실제 계정·토큰·개인 검증 로그는 저장소와 배포 파일에 포함하지 않습니다.

코드 구조(중립 core · provider 경계 · 새 플랫폼 추가 방법)는 [docs/architecture.md](docs/architecture.md)에 있습니다.

`--json` 출력을 스크립트로 다룬다면 [docs/json-schema.md](docs/json-schema.md)(영문, 필드별 타입·의미)와 [docs/exit-codes.md](docs/exit-codes.md)(종료 코드)를 참고하십시오.

<details>
<summary>저장 위치와 CODEX_SWAP_HOME</summary>

저장 위치: `~/.local/share/codex-swap/`. 등록 정보는 `accounts.json`, 추가 계정은 `profiles/`, OpenClaw 변경 백업은 `backups/openclaw/`입니다. 테스트·분리 설치에는 `CODEX_SWAP_HOME`을 지정할 수 있습니다. 이 변수는 셸의 `xswap`과 그 셸의 `codex` 명령이 어떤 `auto.json`을 읽을지 결정합니다 — 등록된 계정, 선택, `codex` 연결 레코드가 모두 그 루트 아래에 있습니다 — 그래서 이 변수가 없는 셸·launchd 작업·데스크톱 앱은 기본 루트를 읽고 거기 있는 선택을 따르거나 아무 선택도 따르지 않습니다. 그 결정이 일어나는 자리에서 루트를 명시합니다: `xswap auto-enable --wrap-codex`는 레코드를 어느 루트에 넣었는지와 변수 없는 셸이 무엇을 읽는지 출력하고, `xswap doctor`는 변수가 루트를 고른 경우 `state root` 행을 추가하며(기본 루트를 가리키는 경우에는 OK), 일반 `codex`는 실제 Codex를 찾지 못할 때 자신이 읽은 루트를 명시합니다. 이 실패가 빈 기본 루트를 새로 만들지도 않습니다. 사용량 서비스가 거부한 로그인 기록은 `auth-state.json`입니다.

</details>

제거 전 `xswap auto-disable`로 codex 링크를 복원한 뒤 `uv tool uninstall intellieffect-xswap`을 실행하십시오. 계정 데이터는 보존됩니다. 기존 계정으로 돌아가려면 `xswap use main --openclaw`를 제거 전에 실행하십시오.

인증 저장 방식의 근거: [OpenAI authentication](https://learn.chatgpt.com/docs/auth). 사용량 조회의 근거: [Codex App Server](https://learn.chatgpt.com/docs/app-server). OpenClaw 연동은 설치된 패키지의 `plugin-sdk/provider-auth`, `plugin-sdk/agent-runtime`, `plugin-sdk/config-runtime`만 사용합니다.

예전 버전(0.3.1/0.3.2)의 브라우저 플러그인 trusted path 복구, weekly 소진 전 전환 릴리스 노트는 [docs/legacy-version-notes.ko.md](docs/legacy-version-notes.ko.md)로 옮겼습니다 — 현재 영문 README에는 대응 절이 없는 한국어 전용 기록입니다.

## 주간 대시보드와 메뉴바

`xswap list`는 고정 번호·계정 이름·이메일 아래에 주간 잔여량 게이지(█ 남음, ░ 사용), 현지 시간대의 초기화 시각과 남은 시간을 트리로 표시합니다. 5h는 기본 화면에서 생략하며, 조회되지 않은 주간 잔여량은 알 수 없음으로 표시합니다. 아래에는 xswap이 감지한 실행 세션을 계정별로 묶어 표시합니다. 선택됨은 새 실행의 기본 계정이며 실제 실행 계정과 구분됩니다. `--details`는 다른 시간대와 리셋권을, `--include-spark`는 Spark 정보를 추가합니다.

`xswap list`는 로케일과 관계없이 영어로 출력합니다. 한국어는 `--lang ko`로 명시합니다. 다른 화면은 기존 언어 설정을 유지합니다.

macOS에서 `xswap menubar`를 실행하면 Apple Command Line Tools로 메뉴 앱을 빌드하고 엽니다. 설치가 필요하면 `xcode-select --install`을 실행하십시오. 메뉴바는 5분마다 갱신하며 새로고침·종료 버튼을 제공합니다. 로그인 시 자동시작은 설정하지 않습니다. 업데이트 후에는 메뉴바를 종료하고 `xswap menubar`를 다시 실행하십시오. 메뉴바 앱은 (실행한 셸이 아니라) 자신이 실행될 때의 환경에서 `--lang`/`XSWAP_LANG`과 같은 규칙으로 영어·한국어를 스스로 판정하며, 내부에서 호출하는 `xswap dashboard`에도 그 판정을 명시적으로 전달합니다.

`xswap switch`/`use`의 브로드캐스트 프로토콜과 재개(resume) 상세는 [docs/session-switching-details.ko.md](docs/session-switching-details.ko.md)에 옮겼습니다.

[English](README.md) | **한국어**

# xswap

Codex CLI·macOS 데스크톱·로컬 OpenClaw에서 사용할 OpenAI 계정을 선택하는 오픈소스 도구입니다. 각 사용자가 **자신의 계정으로 로그인**합니다. 계정이나 구독을 팀원끼리 공유하는 도구가 아닙니다.

## 설치

필수: Python 3.11+, `uv`, 설치된 Codex CLI. OpenClaw 연결 시에는 로컬 `openclaw`와 `node`도 PATH에 있어야 합니다.

```sh
uv tool install 'git+https://github.com/intellieffect/xswap.git@v0.8.0'
xswap --version
```

명령을 찾지 못하면 `uv tool update-shell` 실행 후 새 터미널을 여십시오. `codex-swap`도 동일한 명령입니다. Claude 전용 `cswap`은 변경하지 않습니다.

## 셸 자동완성

zsh는 `xswap completion zsh > "${fpath[1]}/_xswap"` 실행 또는 `.zshrc`에 `eval "$(xswap completion zsh)"` 추가, bash는 `.bashrc`에 `eval "$(xswap completion bash)"` 추가로 설정합니다. 서브커맨드·옵션·계정 이름(`xswap list --offline`으로 로컬에서만 조회, 네트워크 없음) 모두 자동완성됩니다.

## 처음 사용하기

```sh
codex login   # 아직 로그인하지 않았다면 먼저 실행
xswap init    # 그 로그인을 등록하고, 계정을 추가로 물어보고, 자동 전환·정책을 설정한 뒤 doctor까지 실행
```

`xswap init`은 아래 네 단계를 대신 실행하는 얇은 마법사입니다: 현재 `codex login`이 아직 등록되지 않았다면 `main`으로 등록하고, 계정을 추가로 등록할지 물어보고(빈 값이면 건너뜀), 계정이 2개 이상이면 `auto-enable --wrap-codex`를 제안하고, `auto-policy --weekly-remaining 10`을 제안한 뒤 마지막으로 `xswap doctor`를 실행합니다. 각 단계는 건너뛸 수 있고 다시 실행해도 안전합니다. `--yes`를 주면 아무것도 묻지 않고 안전한 기본값만 적용하며(등록 대상이 있으면 `main`으로 등록, 계정 추가는 생략, 자동 전환은 켜지 않음), `--no-auto`는 자동 전환 단계를 건너뛰고, `--weekly-remaining PCT`는 정책 기본값을 덮어씁니다.

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

조회는 공식 Codex App Server의 `account/rateLimits/read`를 사용합니다. 모델 대화나 테스트 턴을 생성하지 않으며, 기본 계정을 전환하지 않습니다. Codex가 필요에 따라 정상 인증 갱신을 수행할 수 있습니다. 계정당 최대 12초를 기다리고 최대 4개 계정을 병렬 조회합니다. 조회 실패·미로그인·API 키 계정은 구분하며, 알 수 없는 값을 `100%`로 표시하지 않습니다. 기본값은 매번 실시간 조회이며 캐시하지 않습니다(아래 「캐시된 조회」 참고).

### 캐시된 조회

`xswap list`·`xswap usage`·`xswap run --best`·`xswap use --best`는 매번 계정당 `codex app-server`를 새로 띄웁니다. 상태표시줄이나 크론처럼 자주 조회하는 호출자를 위해 `--cached SECONDS`를 붙이면 그만큼 신선한 기존 결과를 재사용하고 새로 띄우지 않습니다 — `--cached`를 주지 않으면 기존과 동일하게 항상 실시간 조회입니다. 조회가 성공하면 결과는 항상 `~/.local/share/codex-swap/usage-cache.json`(권한 `0600`)에 계정 이름별로 저장되며, 화면에 보이는 것과 동일한 화이트리스트 필드 — 잔여 비율·초기화 시각·플랜 종류·크레딧, 그리고 이메일일 수도 있는 로컬 계정 라벨 — 만 담고 원본 서버 응답이나 토큰은 담지 않습니다. 저장된 라벨이 현재 로그인 라벨과 다르면(같은 이름으로 재로그인한 경우) 캐시를 재사용하지 않고 새로 조회합니다. `--offline`과 `--cached`는 동시에 쓸 수 없고, `run`·`use`에서는 `--best`와 함께여야 합니다.

사용량 서비스가 로그인을 거부한 경우(조회에 401 계열 응답, 실시간 목록에는 `sign-in required · xswap login 이름`으로 표시)는 `~/.local/share/codex-swap/auth-state.json`(권한 `0600`; 계정 이름·로컬 로그인 라벨·시각·짧은 사유만 담고 토큰은 담지 않음)에도 기록됩니다. 이 기록이 남아 있는 동안 `xswap list`/`usage`의 `--cached`·`--offline`은 Codex를 띄우지 않고 `sign-in required · xswap login 이름`을 표시하고, `run --best`/`use --best`와 자동 전환 풀은 해당 계정을 조회 없이 건너뛰며, `xswap use 이름`은 거부하고, `xswap doctor`가 보고합니다. `--cached` 없는 실시간 `xswap list`/`usage`는 다시 조회하며 성공하면 기록을 지웁니다. `xswap login 이름`도 기록을 지웁니다. 사용량 캐시와 같이, 저장된 라벨이 현재 로그인 라벨과 일치할 때만 유효합니다.

### 잔여량 알림

```sh
xswap list --warn 15   # codex 한도가 15% 미만 남은 창이 하나라도 있으면 경고
```

`--warn PCT`는 1~100 사이 값만 받습니다. 기존 표(또는 `--json`) 출력은 그대로 stdout에 찍히고, 그 뒤 비활성화되지 않은(disabled 아닌) 계정 중 조회에 성공한 계정의 `codex` 한도 창을 검사해 기준치 미만인 창마다 `warn: 이름 창 N% left (resets ...)` 한 줄을 stderr로 출력합니다. 알 수 없는 잔여값은 절대 경고를 발생시키지 않습니다. 경고가 하나라도 발생하면 `xswap list --warn`은 종료코드 `3`을 반환하고, 아니면 `0`을 반환합니다. `--warn` 없는 평범한 `xswap list`는 영향받지 않고 항상 `0`을 반환합니다.

대화형으로 쓰기보다 `launchd`나 `cron`에서 주기적으로 호출하는 용도입니다. `xswap alert --install`이면 그 등록까지 대신 해줍니다.

```sh
xswap alert --install --warn 15 --every 30
```

macOS에서는 `~/.local/share/codex-swap/alert/run.sh`(설치 시점에 확정한 절대경로의 `xswap`을 호출하는 래퍼 — `launchd`의 `PATH`에는 `/opt/homebrew/bin`이 없기 때문이며, `warn:` 줄마다 `osascript` 알림으로 바꿔줍니다)와 `~/Library/LaunchAgents/com.intellieffect.xswap.alert.plist`를 만든 뒤 `launchctl bootstrap`으로 등록합니다. 매 실행 결과는 `alert/last.log`에 남습니다. 상태 확인은 `xswap alert --status`, 제거는 `xswap alert --uninstall`입니다. macOS가 아니면 `--install`은 아무것도 쓰지 않고 대신 동등한 `cron` 한 줄만 출력합니다.

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
uv tool install --force 'git+https://github.com/intellieffect/xswap.git@v0.8.0'
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

`--accounts`는 본인이 로그인한 계정 이름을 우선순위 순서로 지정합니다. 최소 두 개가 필요합니다. 자동 기본 설정을 켜기 전에는 기존 실행 동작을 유지합니다. `xswap run --account 이름`과 `xswap app 이름`은 고정 계정으로 실행합니다. CLI의 대화형 실행·resume·fork·agents를 지원합니다. `codex exec`, `review` 등 `--remote` 훅이 없는 비대화형 명령은 실행 중 전환되지는 않지만, `codex` 명령이 연결된 뒤에는 `xswap run -- exec …`와 같은 규칙(현재 디렉터리 매핑, 없으면 `xswap use`로 선택한 계정)으로 선택 계정의 `CODEX_HOME`을 넣고 셸의 `OPENAI_API_KEY`/`CODEX_API_KEY`/`CODEX_ACCESS_TOKEN`을 제거한 채 한 번 실행되며, stderr에 `xswap: running codex exec as work` 한 줄을 남깁니다(`XSWAP_QUIET=1`은 이 줄만 숨깁니다). `CODEX_HOME`이 이미 설정됐거나 `XSWAP_BYPASS=1`인 경우, `--help`/`--version`/`--remote` 형식, 그리고 `login`, `logout`, `app`, `app-server`, `completion`, `help`, `update`, `upgrade`는 알림 없이 원래 홈으로 전달됩니다(`codex update`·`codex upgrade`가 계정 프로필 안에 릴리스를 설치하면 안 되기 때문입니다). `xswap run -- upgrade`도 자동 풀을 쓰지 않고 선택된 계정의 홈으로 실행되며, 릴리스는 `packages` 링크를 통해 기준 Codex 홈에 설치됩니다. 선택 계정이 비활성·미로그인 등으로 쓸 수 없으면 원래 홈으로 실행하고 이유와 조치를 한 줄 경고로 출력합니다. 잔여량 기준으로 고르려면 `xswap run --best -- exec …`를 사용하십시오. 0.8.0 이전에 저장된 `codex exec` 세션은 `~/.codex/sessions`에 남아 있으며 `XSWAP_BYPASS=1 codex exec resume …`로 이어갈 수 있습니다.

자동 모드 앱과 CLI는 각각 **실행 중인 한 개의 Codex 서버와 대화 저장소를 유지**합니다. CLI는 원본 TUI 프로세스도 그대로 유지하며 사용자 전용 Unix WebSocket으로 서버와 연결합니다. TCP 포트를 열지 않습니다. 앱과 CLI 저장소는 분리되어 있습니다. 새 턴 전에 현재 사용량을 확인하고, 적용되는 한도가 소진되면 잔여량이 확인된 다음 계정으로 인증을 바꿉니다. 한도 정보는 최대 30초 동안 재사용하며, Codex의 한도 알림으로 갱신합니다. 모델별 한도를 확인할 수 없는 후보는 선택하지 않습니다.

진행 중인 턴이 `usageLimitExceeded`로 종료되면 기존 실패 알림을 보존하고 같은 대화에 이어가기 턴을 추가합니다. 원래 사용자 요청을 다시 전송하거나 도구 호출을 재생하지 않습니다. 기존 도구 결과를 확인하며 계속하도록 지시하지만, 모델이 수행하는 외부 작업 자체의 exactly-once 실행을 보장하는 것은 아닙니다. 관측된 다른 턴이 실행 중이면 모두 종료될 때까지 인증 전환을 미룹니다. 사용자가 중단하거나 새 요청을 보내면 대기 중인 자동 이어가기를 취소합니다.

사용 가능한 후보가 없으면 원래 한도 오류를 남기고 멈춥니다. 같은 이어가기 체인에서 동일 계정을 반복 사용하지 않으며, 일반 429/연결 오류/권한 오류/컨텍스트 초과를 한도 소진으로 간주하지 않습니다. 다음 사용자 요청에서는 사용량을 다시 확인할 수 있습니다.

**처음 한 번은 자동 모드로 앱/CLI 세션을 시작해야 합니다.** 이미 실행 중인 일반 창이나 CLI 프로세스에 연결을 끼워 넣지 않습니다. 자동 모드에서 시작한 대화는 이후 계정 전환에도 유지되지만, 기존 계정별 창의 대화는 자동 복사하지 않습니다. 모든 풀 계정은 이 창의 대화·파일 작업 문맥을 공유하므로 개인 계정과 다른 조직의 계정을 섞지 마십시오.

인증은 원래 각 계정의 `auth.json`에서 메모리로 읽고, 만료 시 원래 계정의 공식 CLI를 통해 갱신합니다. 자동 모드 홈에 인증 파일을 복사하지 않으며, 기존 계정 선택·OpenClaw·이미 실행 중인 일반 CLI 세션을 변경하지 않습니다. 토큰 갱신 요청은 같은 계정으로만 처리합니다. `auto/status.json`과 `auto/cli-runs/*/status.json`에는 선택 계정, PID, 전환 횟수, 마지막 상태만 저장하며 토큰·대화 내용은 저장하지 않습니다. 다시 읽을 일이 없는 `auto/cli-runs/` 기록은 `xswap auto-status`, `xswap list`/`usage`의 텍스트 출력(`alert` 작업이 주기마다 정리), 메뉴 막대의 `dashboard` 갱신, 새 브리지 CLI 실행마다 부수 효과로 정리합니다. 브리지가 정상 종료(`reason` 없는 `stopped`)한 기록은 24시간 뒤, 실패 `reason`이 있는 `stopped` 기록이나 `stopped` 기록 없이 브리지가 사라진 기록(강제 종료·크래시·재부팅)은 비정상 종료를 확인할 수 있도록 7일 뒤, 잠금 파일 외에 아무것도 없는 디렉터리(브리지 연결 전에 TUI가 종료된 경우)는 1분 뒤에 제거합니다. `auto-status --prune`은 실행 중이 아닌 기록을 즉시 전부 정리합니다. 어느 경우에도 실행 중인 브리지(잠금 보유)와 생성된 지 60초 미만인 기록은 브리지가 아직 잠금을 잡기 전일 수 있으므로 절대 정리하지 않습니다. `auto-status`는 제거한 기록을 `prunedRuns`(실행 ID·규칙·경과 시간·계정·마지막 이벤트·브리지 버전·종료 사유)로 보고하며, `list --json`/`--short`, `doctor`, `upgrade`는 아무것도 제거하지 않습니다.

각 실행 디렉터리에는 `bridge.log`(0600)도 남습니다. 브리지 이벤트마다 한 줄(로컬 시각, 이벤트, 계정, 요청·후보 계정, 짧은 원인, 실패 시 예외 종류)을 기록하며 토큰·프롬프트·원본 오류는 절대 담지 않고, 1 MiB를 넘으면 오래된 줄부터 버립니다. 실패 원인은 `usage service unavailable`, `ChatGPT access token needs refresh or sign-in`, `selected account is disabled or removed`, `Codex rejected account/login/start`, `app-server request timed out` 같은 짧은 문구로 분류되어 `status.json`의 `reason`·`manualReason`(수동 전환이 대기·실패한 이유, 다음 요청 전까지 유지)·`lastFailure`(세션의 마지막 실패 이벤트)에 기록되고, `xswap auto-status`는 세션별 `log` 경로도 보여줍니다. `xswap use`/`switch`/`login`은 브리지가 요청을 거부한 이유를 `Failed: …`로 출력합니다.

브리지는 앱 서버에 로그인을 보낼 때마다(세션 시작, 자동 전환, `xswap switch`, `xswap login` 재로그인) `account/read`로 서버가 실제로 들고 있는 로그인을 다시 읽어 `verifiedAccount`·`verifiedIdentity`·`verifiedAt`으로 기록합니다(`account`는 브리지가 보냈다고 믿는 계정일 뿐입니다). 서버가 로그인을 갖고 있지 않거나 다른 로그인을 들고 있으면 전환을 `identity mismatch` 사유로 실패 처리하고 이전 계정의 로그인을 다시 보내 세션을 이전 계정에 유지하며, 세션 시작 시점이면 알 수 없는 계정으로 열지 않고 브리지를 중단합니다. 서버가 답할 수 없으면(`account/read`가 없는 Codex CLI, 시간 초과, 이메일 클레임이 없는 토큰) 전환은 유지하고 `verifyReason`에 이유를 남깁니다. 플랜 종류는 신원이 아닙니다. `xswap doctor`의 `auto sessions` 항목이 확인되지 않은 실행 세션과, 확인 이후 다른 사용자로 로그인이 바뀐 계정을 경고합니다.

`codex` 명령이 아직 연결되지 않았으면 `xswap use`가 일반 `codex`가 계속 쓰는 홈과 계정을 출력하고, 계정이 2개 이상이면 `xswap doctor`가 경고합니다.

구현은 데스크톱의 `CODEX_CLI_PATH`, CLI의 `--remote unix://` 연결과 App Server의 실험적 `chatgptAuthTokens` 인터페이스를 사용합니다. 앱 바이너리/설치 파일은 수정하지 않습니다. 이 인터페이스들의 호환성이 바뀔 수 있으며, 오류 시 자동 모드가 중단됩니다. `--wrap-codex`는 사용자 소유 심볼릭 링크만 교체합니다. xswap 밖에서 연결이 풀리는 경우는 두 가지입니다. Codex 업데이트(TUI의 ctrl+u, `codex upgrade`, 설치 스크립트)는 이 링크를 새 릴리스로 되돌리고, Codex 설치는 PATH에서 래핑된 항목보다 앞에 두 번째 `codex`를 추가할 수 있습니다(standalone 설치 스크립트는 `~/.local/bin/codex`를 만듭니다. 2026-09-10에 이 항목이 래핑된 `/opt/homebrew/bin/codex`를 가렸습니다). 자동 전환이 켜져 있으면 다음 `xswap` 실행, `xswap list`/`usage`, `xswap use`/`switch`가 둘 다 복구합니다. 되돌려진 링크는 `xswap-codex`로 다시 연결하고 새 릴리스를 실제 Codex로 기록하며, 래핑된 항목 앞에 새로 생긴 사용자 소유 `codex` 심볼릭 링크도 래핑해 xswap이 실행하는 항목으로 삼습니다(이전 항목은 `auto.json`의 `wrappers`에 보관). 업데이트를 실행한 브리지 세션도 종료 시 복구합니다. `xswap doctor`는 PATH의 모든 `codex`를 검사해 첫 항목이 `xswap-codex`가 아니면 FAIL(항목·대상·다음 실행에서 복구되는지 표시. 일반 파일이나 다른 사용자 소유 링크는 절대 바꾸지 않음), 래핑된 항목 뒤에 전체 경로로만 실행되는 다른 `codex`가 있으면 WARN을 표시하고, `xswap use`도 선택이 우회될 때 같은 내용을 출력합니다. `xswap auto-disable`은 래핑된 모든 항목을 현재 릴리스로 되돌립니다. PATH는 xswap을 실행한 셸의 것이며 launchd·cron 작업은 자기 PATH를 봅니다. xswap은 확신할 수 없는 항목은 고쳐 쓰지 않습니다: 링크 대상이 없거나 실행 파일이 아니거나, 항목이 일반 파일이거나 없거나 다른 사용자 소유이거나, 자동 전환이 꺼져 있으면 항목을 그대로 두고 `xswap doctor`와 `xswap use`/`switch`가 어느 경우인지와 수동 조치 방법을 알려줍니다(`xswap auto-status`에는 `wrapperReason`으로 표시됩니다). 일반 모드로 돌아가려면 `xswap auto-disable`을 실행하십시오. 이미 실행 중인 자동 세션은 계속 유지됩니다.

이 업데이터는 설치 위치를 `CODEX_HOME`에서 정하는데, 브리지 세션은 `CODEX_HOME`을 xswap 런타임 홈으로 두므로 세션 안에서 시작한 업데이트는 `~/.local/share/codex-swap/auto/cli-codex/packages`에 설치되어 `auto/`를 정리하면 함께 사라집니다. xswap은 자신이 소유한 홈(`auto/cli-codex`, `auto/codex`, `profiles/<이름>/codex`)의 `packages`를 기준 Codex 홈(`~/.codex` 또는 `CODEX_HOME`, 혹은 실제 Codex를 이미 보유한 등록 홈)의 `packages`로 링크해 업데이트가 원래 위치에 설치되게 하고, 실제 Codex를 그 홈 경로로 기록합니다. xswap 상태 디렉터리 안에 남아 있는 실제 Codex는 `xswap doctor`가 `real codex` FAIL로 보고하며, `xswap relocate-codex`(`--dry-run`으로 미리보기)가 릴리스를 기준 홈으로 옮기고 `current`와 `auto.json`을 다시 씁니다. 릴리스를 삭제하지 않고, 대상이 이미 있으면 거부하며, 셸 설정은 수정하지 않습니다. 일반 `codex`가 실제 Codex 실행 파일이 없다고 알리면 `xswap auto-disable` 후 Codex를 다시 설치하고 `xswap auto-enable --accounts … --wrap-codex`를 실행하십시오.

실제 Codex CLI 0.153.4와 macOS 앱 내장 바이너리에 가짜 계정/로컬 HTTP 서버를 연결하여 검증했습니다. `첫 계정 응답 → 한도 오류 → 두 번째 계정 응답`에서 서버 PID·대화 ID가 유지되고 3개 턴이 보존됐습니다. 실제 TUI를 PTY로 실행한 검사에서도 TUI/서버 PID와 대화 ID가 유지되며 자동 전환 후 다음 사용자 요청을 처리하고 Ctrl-C로 정상 종료됐습니다. 실제 구독 한도를 소진시키는 테스트는 하지 않습니다.

## OpenClaw도 함께 전환하기

```sh
xswap openclaw work --dry-run       # 적용할 계정·에이전트 확인
xswap openclaw work                 # 로컬 OpenClaw 전체 에이전트에 적용
xswap use main --openclaw           # CLI·앱 기본 계정과 OpenClaw를 함께 전환
xswap openclaw --pool main,work     # OpenClaw가 두 계정을 스스로 순환하도록 등록
```

`xswap openclaw`는 이름을 생략하면 현재 선택 계정을 사용합니다. 특정 에이전트만 바꾸려면 `xswap openclaw work --agent main --agent devagent`처럼 지정하십시오. 이 명령 자체는 xswap의 기본 계정을 바꾸지 않습니다.

`--pool a,b,c`는 등록된 이름 2개 이상을 쉼표로 지정하며, 위치 인자 NAME과는 함께 쓸 수 없습니다. 나열한 순서 그대로 각 에이전트의 OpenAI 인증 순서에 전부 등록하면, 이후 OpenClaw가 자신의 쿨다운 로직(`resolveAuthProfileOrder`·`isProfileInCooldown`·`markAuthProfileFailure`/`markAuthProfileCooldown`)으로 그 안에서 스스로 순환합니다. 즉 한 계정이 한도에 걸려도 사람이 `xswap openclaw other`를 실행할 때까지 기다릴 필요가 없습니다. 풀에 묶인 에이전트는 그 순간 선택된 계정이 무엇이든 동일한 대화·작업 맥락을 공유하므로, 서로 다른 ChatGPT 조직(계정)을 섞으면 그 조직들의 맥락이 한 대화 안에서 뒤섞입니다 — `sync_openclaw`는 풀에 속한 계정들의 조직이 갈리면 기본적으로 중단하며, 의도한 것이면 `--allow-mixed`로 넘길 수 있습니다. 계정의 조직 자체를 확인할 수 없는 경우(인증 파일을 읽을 수 없거나 해독 가능한 클레임이 없는 경우)도 실제로 조직이 다를 때와 동일하게 중단합니다 — 확인 불가를 일치로 간주하지 않습니다.

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

`xswap remove`는 계정의 레지스트리 항목만 지우고 파일은 기본적으로 남겨두며, `--purge`를 추가하면 관리형 프로필 디렉터리까지 삭제합니다(등록된 홈, 즉 사용자의 `~/.codex`는 어떤 플래그를 줘도 삭제하지 않습니다). 활성화된 `--auto`/`auto-enable` 풀에 속했거나 실행 중인 자동 세션이 사용 중인 계정은 거부하며, `--yes` 없이는 확인을 묻습니다.

## 지원 범위와 동작 원리

Codex 인증 저장은 `file` 방식만 지원합니다. `keyring`/`auto`는 변경 없이 오류로 중단합니다. CLI 프로필은 macOS/Linux에서 사용할 수 있으며 데스크톱 실행은 macOS 전용입니다. Windows는 지원하지 않습니다.

OpenClaw 연결은 **ChatGPT OAuth + 로컬 Gateway** 전용입니다. API 키 인증이나 원격 Gateway를 이 명령으로 교체하지 않습니다. OpenClaw 2026.8.1에서 실제 검증했으며, 공개 SDK가 없는 버전에서는 중단합니다. Gateway가 실행 중이어야 인증 재적용까지 완료됩니다.

`xswap use`는 이후 **xswap으로 실행하는** CLI와 앱에 적용됩니다. 일반 `codex` 명령과 이미 열린 앱·CLI 세션에는 소급 적용되지 않습니다. `--openclaw`를 붙이지 않으면 OpenClaw는 변경하지 않습니다. 명시적으로 계정을 고정한 기존 OpenClaw 세션이나 진행 중인 작업은 기존 인증을 유지할 수 있습니다. 계정을 명시하지 않았을 때 정확히 다음 명령만 디렉터리 매핑을 따릅니다: 계정 없는 `xswap`·`--account` 없는 `xswap run`·`xswap app`·`xswap status`·`xswap usage`(가장 깊이 일치하는 매핑 우선, 매핑이 없을 때만 `xswap use`로 선택한 계정으로 돌아감. 우선순위: 명시적 계정 > 디렉터리 매핑 > 선택된 계정). `xswap openclaw`는 이름을 생략해도 디렉터리 매핑을 절대 따르지 않고 항상 `xswap use`로 선택한 계정을 사용합니다 — 하나의 실행 세션이 아니라 로컬 OpenClaw 에이전트 전체의 공유 인증 상태를 바꾸기 때문입니다. 비활성화(`disable`)된 계정에 매핑돼 있어도 해당 매핑은 그대로 적용되어 실행되며, 이는 `disable`이 `use`·`--auto`/`auto-enable` 풀·`openclaw`·실시간 사용량 조회에만 영향을 준다는 기존 규칙과 일치합니다.

현재 계정을 등록할 때는 기존 `CODEX_HOME`을 참조합니다. 추가 계정은 독립된 인증·대화·DB를 사용하며, `config.toml`, `AGENTS.md`, `skills`, `rules`는 원래 홈을 공유합니다. `plugins`는 각 홈에 독립된 코드 캐시를 생성하고 이후에는 해당 홈의 플러그인 관리자가 갱신합니다. 다른 홈의 플러그인 변경을 자동 덮어쓰지 않습니다. **계정 분리는 파일·도구 접근 권한을 격리하는 보안 샌드박스가 아닙니다.**

OpenClaw 연결은 실행 시점의 인증을 동기화합니다. 이미 OpenClaw에 더 최신 토큰이 있으면 이를 보존하고, 동일 만료 시각에 서로 다른 토큰이면 덮어쓰지 않고 중단합니다. Codex와 OpenClaw 사이에 상주 동기화 데몬을 설치하지 않습니다. 다른 도구에서 재로그인한 뒤에는 `xswap openclaw 이름`을 다시 실행하십시오.

데스크톱은 계정별 `CODEX_HOME`과 `CODEX_ELECTRON_USER_DATA_PATH`로 실행합니다. ChatGPT 26.901.20858의 별도 프로필 실행과 내장 Codex의 인증 인식을 검증했습니다. 이 환경변수는 공식 안정 API로 보장되지 않으므로 앱 업데이트 후 새 창의 프로필 메뉴에서 계정을 확인하십시오.

## 오류가 나면

먼저 `xswap doctor`를 실행하십시오. 네트워크 호출 없이 읽기 전용으로 codex 실행 파일, PATH의 모든 `codex`와 래퍼 연결 상태, 인증 저장 방식, 등록 계정별 로그인·토큰 만료·사용량 조회에서 거부된 로그인 기록, 플러그인 링크, 자동 전환 풀, 실제 Codex 실행 파일 위치(`real codex`, `packages link`), 설치된 xswap보다 낮은 브리지로 실행 중인 세션, 실행 중인 자동 세션의 서버 확인 로그인, OpenClaw plugin SDK를 점검합니다. 지금 xswap 선택을 우회하는 `codex` 항목은 FAIL로 보고하므로, 다음 `xswap` 실행·`list`·`use`가 다시 연결할 때까지 `xswap doctor`는 종료 코드 1을 반환합니다. `xswap doctor --json`은 자동화용 출력이며, 하나라도 실패(FAIL)하면 종료 코드 1을 반환합니다.

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
python3 tests/live_codex_smoke.py  # 실제 Codex + 가짜 계정/로컬 서버 검증
uv tool install .
```

CI는 macOS/Linux, Python 3.11/3.14, Node 22에서 테스트와 설치를 확인합니다. 자동 테스트는 가짜 인증만 사용합니다. 실제 계정·토큰·개인 검증 로그는 저장소와 배포 파일에 포함하지 않습니다.

저장 위치: `~/.local/share/codex-swap/`. 등록 정보는 `accounts.json`, 추가 계정은 `profiles/`, OpenClaw 변경 백업은 `backups/openclaw/`입니다. 테스트·분리 설치에는 `CODEX_SWAP_HOME`을 지정할 수 있습니다. 사용량 서비스가 거부한 로그인 기록은 `auth-state.json`입니다.

제거 전 `xswap auto-disable`로 codex 링크를 복원한 뒤 `uv tool uninstall intellieffect-xswap`을 실행하십시오. 계정 데이터는 보존됩니다. 기존 계정으로 돌아가려면 `xswap use main --openclaw`를 제거 전에 실행하십시오.

인증 저장 방식의 근거: [OpenAI authentication](https://learn.chatgpt.com/docs/auth). 사용량 조회의 근거: [Codex App Server](https://learn.chatgpt.com/docs/app-server). OpenClaw 연동은 설치된 패키지의 `plugin-sdk/provider-auth`, `plugin-sdk/agent-runtime`, `plugin-sdk/config-runtime`만 사용합니다.


## 브라우저 플러그인의 trusted path 복구 (0.3.1)

0.3.0 이하의 `plugins` 공유 링크는 실제 코드가 실행 홈 밖으로 해석되어
`Trusted RPC dependency must resolve within a configured trusted code path`를 발생시킬 수 있습니다.
앱은 실행 중인 `CODEX_HOME`을 신뢰 루트로 등록하며 서비스는 파일의 realpath를 검사합니다.
따라서 원본 browser-client를 import해도 프로필 경로에 등록된 서비스 로딩은 실패할 수 있습니다.
계정별 `profiles/NAME/codex`와 자동 모드의 `auto/codex`, `auto/cli-codex` 모두 복구 대상입니다.
자동 모드의 `state_5.sqlite` 경로는 대화 저장소 경로이며 오류의 직접 원인은 플러그인 링크입니다.

```sh
xswap repair-plugins --dry-run
xswap repair-plugins
```

새 프로필/자동 세션은 처음부터 로컬 플러그인 캐시를 만듭니다. 기존 링크는 완성된 로컬
디렉터리와 원자적으로 교환하여 경로가 사라지는 틈 없이 복구합니다. 원본 플러그인과
이미 로컬인 캐시는 유지합니다. 이미 초기화에 실패한 브라우저 런타임은 실패 결과가 캐시되므로 복구 후 해당 세션에서 브라우저 도구의 `js_reset`을 한 번 호출한 뒤 다시 초기화해야 합니다. 이는 JavaScript 변수/도구 런타임만 초기화하며 Codex 대화·앱·CLI 서버는 유지합니다. 복구 명령은 실행 중인 도구를 강제로 초기화하지 않습니다. 복구는 프로세스를 종료하거나 신뢰 경로를 확장하지 않으며,
인증·대화·브라우저 세션·플러그인 임시 상태를 복사하지 않습니다. 알려진 플러그인 실행
헬퍼 두 개는 로컬에 보존합니다. 이전 링크 경로는 `.xswap-plugins-migration.json`에 기록합니다.
외부/손상/순환 캐시 링크는 복사하지 않고 오류로 중단합니다.

회귀 검사: `uv run python tests/plugin_runtime_smoke.py`는 앱에 포함된 실제 런타임에서
기존 링크 오류, 실패 캐시로 인한 재시도 실패, `js_reset` 후 같은 도구 서버에서의 복구를 검증합니다. `XSWAP_TEST_BROWSER=1`을
`tests/live_cli_smoke.py` / `tests/live_codex_smoke.py` 실행에 지정하면 모의 계정 전환
전후 실제 브라우저 서비스 초기화도 검사합니다. 이 검사는 브라우저 탭 탐색이나
aside/iab 호스트 연결 상태를 검증하지 않습니다.


## Weekly 사용량 소진 전 전환 (0.3.2)

```sh
xswap auto-policy --weekly-remaining 10   # weekly 90% 사용 / 10% 잔여 시 전환
xswap auto-status                       # 설정값과 실행 브리지 버전 확인
xswap auto-policy --weekly-remaining 0    # 완전 소진 때만 전환 (기존 기본값)
```

기준값은 0 이상 100 미만의 잔여 퍼센트입니다. weekly는 primary/secondary 이름이 아닌
7일(10,080분) 기간으로 식별합니다. 적용되는 weekly 잔여량이 기준 이하이면, 관측된
실행 중인 턴이 모두 끝난 뒤 다음 턴을 시작하기 전에 기준을 초과하는 계정으로 전환합니다.
작업 중간을 강제로 끊지 않으므로 긴 턴이 한도를 소진할 가능성은 여전히 있으며,
이 경우 기존 한도 오류 후 이어가기 기능이 적용됩니다. 사용량 캐시는 최대 30초입니다.

후보의 weekly 잔여량을 확인할 수 없거나 기준 이하이면 선택하지 않습니다.
적합한 후보가 없으면 현재 계정을 유지해 남은 양을 사용할 수 있고, 계정을 왕복 전환하지
않습니다. 다른 시간대 한도의 완전 소진 검사도 유지됩니다.

0.3.2 브리지는 매 턴 설정을 다시 읽으므로 이후 기준 변경에 앱/CLI 재시작이 필요 없습니다.
0.3.1 이하로 이미 실행된 브리지는 이 기능이 없어서 자동 세션을 한 번 새로 시작해야 합니다.
설정/설치는 기존 세션을 종료하지 않습니다. `auto-status`의 전역 값은 저장된 기준이며,
각 세션의 `bridgeVersion`/`weeklyRemainingThreshold`는 실제 실행 코드의 지원·상태를 나타냅니다.

`xswap upgrade` 시점에 이미 열려 있던 세션은 다시 열 때까지 이전 브리지를 계속 실행합니다. 각 브리지는
현재 대화(`conversationId`), `codexHome`, 풀(`accounts`)을 `status.json`에 기록하므로, `xswap list`는 실행
세션 아래에 다시 여는 명령을 표시하고(`⚠ bridge 0.7.8 · reopen with codex resume UUID to load 0.8.0`,
`codex` 명령이 연결돼 있지 않으면 `xswap run -- resume UUID`), `xswap doctor`는 `auto cli-runs` 행을
WARN으로 보고하며, `xswap upgrade`는 설치 성공 후 같은 줄을 출력합니다. 대화는 0.8.0 이상 브리지가 한 번
이상 턴을 저장한 뒤에만 이름으로 지목됩니다. 그 전의 기록이나 저장된 대화가 없는 세션은 `exit and reopen it`,
데스크톱 세션은 `quit and reopen it with xswap app`으로 표시됩니다. 이전 세션이 대화의 쓰기 잠금을 아직
쥐고 있으므로 다시 열기 전에 종료하십시오. 아무 세션도 자동으로 중단·재시작되지 않습니다.

`XSWAP_TEST_RESERVE=1`로 실제 Codex 서버/CLI 회귀 검사를 실행하면, 모의 weekly 10% 알림 후
한도 오류 없이 다음 계정이 다음 사용자 턴을 처리하는지 확인합니다.


라이선스: [MIT](LICENSE). 보안 및 개인정보: [SECURITY.md](SECURITY.md). 기여 안내: [CONTRIBUTING.md](CONTRIBUTING.md). OpenAI의 공식 제품이 아니며, Codex·ChatGPT·OpenClaw의 라이선스는 각 제공자에게 있습니다.

## 주간 대시보드와 메뉴바

`xswap list`는 고정 번호·계정 이름·이메일 아래에 주간 잔여량 게이지(█ 남음, ░ 사용), 현지 시간대의 초기화 시각과 남은 시간을 트리로 표시합니다. 5h는 기본 화면에서 생략하며, 조회되지 않은 주간 잔여량은 알 수 없음으로 표시합니다. 아래에는 xswap이 감지한 실행 세션을 계정별로 묶어 표시합니다. 선택됨은 새 실행의 기본 계정이며 실제 실행 계정과 구분됩니다. `--details`는 다른 시간대와 리셋권을, `--include-spark`는 Spark 정보를 추가합니다.

`xswap list`는 로케일과 관계없이 영어로 출력합니다. 한국어는 `--lang ko`로 명시합니다. 다른 화면은 기존 언어 설정을 유지합니다.

macOS에서 `xswap menubar`를 실행하면 Apple Command Line Tools로 메뉴 앱을 빌드하고 엽니다. 설치가 필요하면 `xcode-select --install`을 실행하십시오. 메뉴바는 5분마다 갱신하며 새로고침·종료 버튼을 제공합니다. 로그인 시 자동시작은 설정하지 않습니다. 업데이트 후에는 메뉴바를 종료하고 `xswap menubar`를 다시 실행하십시오. 메뉴바 앱은 (실행한 셸이 아니라) 자신이 실행될 때의 환경에서 `--lang`/`XSWAP_LANG`과 같은 규칙으로 영어·한국어를 스스로 판정하며, 내부에서 호출하는 `xswap dashboard`에도 그 판정을 명시적으로 전달합니다.

`xswap switch NAME`(`xswap use NAME`과 동일)은 기본 계정을 선택하고 **실행 중인 호환 자동 모드 CLI·데스크톱 브리지 전체에 전환을 전달**합니다. 대기 중인 세션은 바로 적용하고, 응답 중인 세션은 모든 턴이 끝난 뒤 적용합니다. 서버 프로세스와 대화는 유지됩니다. 자동 풀 밖의 등록 계정도 수동 선택할 수 있으며, 이후 턴의 자동 전환 풀·잔여량 정책은 그대로 적용됩니다.

```sh
xswap switch work
xswap auto-status                    # manualState: pending / applying / applied / failed
xswap switch work --default-only      # 새 실행의 기본값만 변경
```

결과는 적용 완료(`applied`), 대기(`pending`), 미지원(`unsupported`), 실패(`failed`), 수신 미확인(`unconfirmed`) 건수로 표시합니다. 실제 인증 갱신 성공 응답이 있어야 적용 완료로 셉니다. 명령은 최대 2초 동안 응답을 확인하며, 대기 중인 요청의 후속 상태는 `auto-status`에서 확인합니다. 실패·수신 미확인은 종료 코드 1을 반환하며 저장된 기본 계정은 유지됩니다. 바쁜 세션에 반복 요청하면 마지막 선택이 대기 요청을 대체합니다.

**이전 버전으로 이미 실행 중인 브리지와 일반 고정 계정 세션은 수신할 수 없습니다.** 업데이트된 설치본으로 자동 모드 세션을 한 번 열어야 합니다. `manualSwitchVersion: 1`이 지원 여부를 나타냅니다. 설치는 실행 중인 프로세스를 교체하거나 종료하지 않습니다. 계정별 인증 파일을 복사하지 않으며, 비공개 로컬 요청 파일에는 계정 이름과 프로세스별 식별자만 기록합니다.

자동 모드의 `codex resume UUID` / `fork UUID`는 자동 런타임·기존 Codex 홈·등록 계정 홈에서 해당 대화를 찾고, 대화를 복사하지 않고 원래 홈에서 인증 브리지와 함께 재개합니다. 선택 화면·대화 이름·`--last`는 자동 런타임 범위를 유지합니다. 자동 런타임 밖 여러 홈에 같은 UUID가 있으면 원래 홈을 명시해야 합니다.

자동 모드 CLI 종료 후에는 Codex의 임시 원격 주소 아래에 마지막으로 표시되는 xswap 재개 명령을 사용하십시오. 종료된 소켓 대신 새 브리지를 열며 계정 풀·현재 계정·원래 세션 홈을 유지합니다.

`--details`에서 리셋권은 `codex reset credits: N available`로 별도 표시하며, 서버가 제공한 사용 가능한 리셋권의 만료일도 보여줍니다. 정보를 제공하지 않으면 `unknown`으로 표시합니다(0개와 구분). `usage --json`에는 `resetCredits`가 포함됩니다. 조회는 리셋권을 사용하지 않습니다.

세션 안의 `/resume` 선택창은 별도 조회 연결로 실행 중인 서버를 공유합니다. 선택창을 닫아도 기존 대화와 계정 자동전환은 유지됩니다. 업데이트 전에 실행한 CLI 세션은 새 브리지를 적용하려면 한 번 다시 실행해야 합니다.

자동 모드의 재개 목록은 다른 서버가 쓰고 있는 대화를 제외합니다. UUID 직접 재개도 대화가 다른 창에 열려 있으면 TUI 시작 전에 안내합니다. 기존 창으로 돌아가거나 해당 세션을 종료한 뒤 재개하십시오. 잠금 파일은 삭제하지 않습니다. 종료 후 재개 안내에는 실제 저장된 대화 ID만 사용합니다.

CLI 계정 전환 로그는 실행 중인 TUI 화면에 직접 출력하지 않으며 `xswap auto-status`와 실행 디렉터리의 `bridge.log`에서 확인할 수 있습니다. 실패 이벤트(`manual-switch-failed`, `candidate-unavailable`, `no-available-account`, `continuation-failed`, `refresh-failed`, `quota-check-failed`)에는 짧게 분류한 `reason`이 붙고 그중 가장 최근 항목이 `lastFailure`에 남습니다. 브리지가 죽은 `stopped` 기록도 자체 `reason`을 담지만 `lastFailure`를 덮지 않으므로, 전환 실패 뒤 브리지가 죽어도 원래 원인이 남습니다. 연결 오류 안내는 TUI 종료 후 출력하며, 세션이 실패했거나 실패를 기록했다면 그 안내에 `bridge.log` 경로를 함께 표시합니다.

`xswap switch NAME` / `use NAME`은 자동 모드 풀에서도 해당 계정을 맨 앞으로 옮겨 새 CLI·앱 세션에 적용합니다. 실행 중인 호환 브리지는 유휴 상태에서 적용하고 진행 중인 턴은 종료 후 적용합니다. `--default-only`는 기존 세션에 전달하지 않고 새 실행만 변경합니다.

`xswap switch 1` / `xswap switch 2`로 목록의 번호를 선택할 수 있으며 `xswap switch main`처럼 이름도 사용할 수 있습니다. 비활성화 계정은 자리를 유지하지만 선택할 수 없습니다. 계정 삭제 시 뒤쪽 번호는 다시 매겨집니다. 숫자로 된 계정 이름은 `xswap use NAME`으로 선택합니다.

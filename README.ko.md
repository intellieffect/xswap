[English](README.md) | **한국어**

# xswap

Codex CLI·macOS 데스크톱·로컬 OpenClaw에서 사용할 OpenAI 계정을 선택하는 오픈소스 도구입니다. 각 사용자가 **자신의 계정으로 로그인**합니다. 계정이나 구독을 팀원끼리 공유하는 도구가 아닙니다.

## 설치

필수: Python 3.11+, `uv`, 설치된 Codex CLI. OpenClaw 연결 시에는 로컬 `openclaw`와 `node`도 PATH에 있어야 합니다.

```sh
uv tool install 'git+https://github.com/intellieffect/xswap.git@v0.4.3'
xswap --version
```

명령을 찾지 못하면 `uv tool update-shell` 실행 후 새 터미널을 여십시오. `codex-swap`도 동일한 명령입니다. Claude 전용 `cswap`은 변경하지 않습니다.

## 처음 사용하기

```sh
xswap register main       # 현재 Codex 로그인 등록
xswap add work            # 브라우저에서 추가 OpenAI 계정으로 로그인
xswap use work            # xswap의 기본 계정 선택
xswap                     # 선택한 계정으로 Codex CLI 실행
xswap app                 # 선택한 계정의 별도 데스크톱 창 열기
xswap login work          # work 계정의 로그인이 만료됐을 때 재인증
```

현재 Codex에 로그인하지 않았다면 먼저 `codex login`을 실행하십시오. `xswap list`로 등록 계정을, `xswap status`로 선택된 계정의 로컬 로그인 상태를 확인할 수 있습니다.

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

조회는 공식 Codex App Server의 `account/rateLimits/read`를 사용합니다. 모델 대화나 테스트 턴을 생성하지 않으며, 기본 계정을 전환하지 않습니다. Codex가 필요에 따라 정상 인증 갱신을 수행할 수 있습니다. 계정당 최대 12초를 기다리고 최대 4개 계정을 병렬 조회합니다. 조회 실패·미로그인·API 키 계정은 구분하며, 알 수 없는 값을 `100%`로 표시하지 않습니다. 결과는 매번 조회하고 캐시하지 않습니다.

기존 버전 업데이트:

```sh
uv tool install --force 'git+https://github.com/intellieffect/xswap.git@v0.4.3'
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

`--accounts`는 본인이 로그인한 계정 이름을 우선순위 순서로 지정합니다. 최소 두 개가 필요합니다. 자동 기본 설정을 켜기 전에는 기존 실행 동작을 유지합니다. `xswap run --account 이름`과 `xswap app 이름`은 고정 계정으로 실행합니다. CLI의 대화형 실행·resume·fork·agents를 지원하며, `codex exec`, login/logout 등 비대화형 명령과 명시적 `--remote` 연결은 원래 Codex로 전달되어 자동 전환 대상이 아닙니다.

자동 모드 앱과 CLI는 각각 **실행 중인 한 개의 Codex 서버와 대화 저장소를 유지**합니다. CLI는 원본 TUI 프로세스도 그대로 유지하며 사용자 전용 Unix WebSocket으로 서버와 연결합니다. TCP 포트를 열지 않습니다. 앱과 CLI 저장소는 분리되어 있습니다. 새 턴 전에 현재 사용량을 확인하고, 적용되는 한도가 소진되면 잔여량이 확인된 다음 계정으로 인증을 바꿉니다. 한도 정보는 최대 30초 동안 재사용하며, Codex의 한도 알림으로 갱신합니다. 모델별 한도를 확인할 수 없는 후보는 선택하지 않습니다.

진행 중인 턴이 `usageLimitExceeded`로 종료되면 기존 실패 알림을 보존하고 같은 대화에 이어가기 턴을 추가합니다. 원래 사용자 요청을 다시 전송하거나 도구 호출을 재생하지 않습니다. 기존 도구 결과를 확인하며 계속하도록 지시하지만, 모델이 수행하는 외부 작업 자체의 exactly-once 실행을 보장하는 것은 아닙니다. 관측된 다른 턴이 실행 중이면 모두 종료될 때까지 인증 전환을 미룹니다. 사용자가 중단하거나 새 요청을 보내면 대기 중인 자동 이어가기를 취소합니다.

사용 가능한 후보가 없으면 원래 한도 오류를 남기고 멈춥니다. 같은 이어가기 체인에서 동일 계정을 반복 사용하지 않으며, 일반 429/연결 오류/권한 오류/컨텍스트 초과를 한도 소진으로 간주하지 않습니다. 다음 사용자 요청에서는 사용량을 다시 확인할 수 있습니다.

**처음 한 번은 자동 모드로 앱/CLI 세션을 시작해야 합니다.** 이미 실행 중인 일반 창이나 CLI 프로세스에 연결을 끼워 넣지 않습니다. 자동 모드에서 시작한 대화는 이후 계정 전환에도 유지되지만, 기존 계정별 창의 대화는 자동 복사하지 않습니다. 모든 풀 계정은 이 창의 대화·파일 작업 문맥을 공유하므로 개인 계정과 다른 조직의 계정을 섞지 마십시오.

인증은 원래 각 계정의 `auth.json`에서 메모리로 읽고, 만료 시 원래 계정의 공식 CLI를 통해 갱신합니다. 자동 모드 홈에 인증 파일을 복사하지 않으며, 기존 계정 선택·OpenClaw·이미 실행 중인 일반 CLI 세션을 변경하지 않습니다. 토큰 갱신 요청은 같은 계정으로만 처리합니다. `auto/status.json`과 `auto/cli-runs/*/status.json`에는 선택 계정, PID, 전환 횟수, 마지막 상태만 저장하며 토큰·대화 내용은 저장하지 않습니다.

구현은 데스크톱의 `CODEX_CLI_PATH`, CLI의 `--remote unix://` 연결과 App Server의 실험적 `chatgptAuthTokens` 인터페이스를 사용합니다. 앱 바이너리/설치 파일은 수정하지 않습니다. 이 인터페이스들의 호환성이 바뀔 수 있으며, 오류 시 자동 모드가 중단됩니다. `--wrap-codex`는 사용자 소유 심볼릭 링크만 교체합니다. Codex 업데이트가 링크를 변경하면 다시 연결해야 할 수 있습니다. 일반 모드로 돌아가려면 `xswap auto-disable`을 실행하십시오. 이미 실행 중인 자동 세션은 계속 유지됩니다.

실제 Codex CLI 0.153.4와 macOS 앱 내장 바이너리에 가짜 계정/로컬 HTTP 서버를 연결하여 검증했습니다. `첫 계정 응답 → 한도 오류 → 두 번째 계정 응답`에서 서버 PID·대화 ID가 유지되고 3개 턴이 보존됐습니다. 실제 TUI를 PTY로 실행한 검사에서도 TUI/서버 PID와 대화 ID가 유지되며 자동 전환 후 다음 사용자 요청을 처리하고 Ctrl-C로 정상 종료됐습니다. 실제 구독 한도를 소진시키는 테스트는 하지 않습니다.

## OpenClaw도 함께 전환하기

```sh
xswap openclaw work --dry-run       # 적용할 계정·에이전트 확인
xswap openclaw work                 # 로컬 OpenClaw 전체 에이전트에 적용
xswap use main --openclaw           # CLI·앱 기본 계정과 OpenClaw를 함께 전환
```

`xswap openclaw`는 이름을 생략하면 현재 선택 계정을 사용합니다. 특정 에이전트만 바꾸려면 `xswap openclaw work --agent main --agent devagent`처럼 지정하십시오. 이 명령 자체는 xswap의 기본 계정을 바꾸지 않습니다.

OpenClaw 연결은 다음을 수행합니다.

1. 선택 계정의 ChatGPT OAuth 인증을 확인합니다.
2. 변경 대상 인증·우선순위만 작은 0600 파일로 백업합니다.
3. OpenClaw의 공개 SDK와 잠금·트랜잭션을 통해 인증을 등록하고 대상 에이전트의 OpenAI 인증 선택을 변경합니다.
4. `openclaw secrets reload`로 실행 중인 Gateway 인증 상태를 재적용합니다.

원래 인증 프로필은 보존합니다. 선택된 에이전트의 OpenAI 인증 순서는 해당 계정 하나로 지정하므로 다른 계정으로 자동 순환하지 않습니다. 기존 모델·채널 설정이나 대화 기록은 변경하지 않으며, 테스트 메시지를 자동 전송하지도 않습니다. 정상 완료는 **저장·선택·Gateway 재적용 확인**을 뜻하며 모델의 실제 응답이나 잔여 사용량을 보장하지 않습니다.

백업 경로를 바꾸려면 `xswap openclaw work --backup-dir /path/to/private-backups`를 사용하십시오. 전체 미디어·대화 DB를 백업하지 않습니다.

## 자주 쓰는 명령

| 명령 | 동작 |
|---|---|
| `xswap list` | 계정 목록·잔여 사용량·초기화 시간 표시 |
| `xswap run --account main -- resume` | 지정 계정으로 Codex 명령 실행 |
| `xswap app work` | 지정 계정의 macOS 앱 실행 |
| `xswap add work --device-auth` | Codex의 기기 코드 로그인 사용 |
| `xswap app work --dry-run` | 앱 실행 경로와 환경변수 확인 |
| `xswap login work` | 등록된 계정을 재인증(로그인 만료 시) |

## 지원 범위와 동작 원리

Codex 인증 저장은 `file` 방식만 지원합니다. `keyring`/`auto`는 변경 없이 오류로 중단합니다. CLI 프로필은 macOS/Linux에서 사용할 수 있으며 데스크톱 실행은 macOS 전용입니다. Windows는 지원하지 않습니다.

OpenClaw 연결은 **ChatGPT OAuth + 로컬 Gateway** 전용입니다. API 키 인증이나 원격 Gateway를 이 명령으로 교체하지 않습니다. OpenClaw 2026.8.1에서 실제 검증했으며, 공개 SDK가 없는 버전에서는 중단합니다. Gateway가 실행 중이어야 인증 재적용까지 완료됩니다.

`xswap use`는 이후 **xswap으로 실행하는** CLI와 앱에 적용됩니다. 일반 `codex` 명령과 이미 열린 앱·CLI 세션에는 소급 적용되지 않습니다. `--openclaw`를 붙이지 않으면 OpenClaw는 변경하지 않습니다. 명시적으로 계정을 고정한 기존 OpenClaw 세션이나 진행 중인 작업은 기존 인증을 유지할 수 있습니다.

현재 계정을 등록할 때는 기존 `CODEX_HOME`을 참조합니다. 추가 계정은 독립된 인증·대화·DB를 사용하며, `config.toml`, `AGENTS.md`, `skills`, `rules`는 원래 홈을 공유합니다. `plugins`는 각 홈에 독립된 코드 캐시를 생성하고 이후에는 해당 홈의 플러그인 관리자가 갱신합니다. 다른 홈의 플러그인 변경을 자동 덮어쓰지 않습니다. **계정 분리는 파일·도구 접근 권한을 격리하는 보안 샌드박스가 아닙니다.**

OpenClaw 연결은 실행 시점의 인증을 동기화합니다. 이미 OpenClaw에 더 최신 토큰이 있으면 이를 보존하고, 동일 만료 시각에 서로 다른 토큰이면 덮어쓰지 않고 중단합니다. Codex와 OpenClaw 사이에 상주 동기화 데몬을 설치하지 않습니다. 다른 도구에서 재로그인한 뒤에는 `xswap openclaw 이름`을 다시 실행하십시오.

데스크톱은 계정별 `CODEX_HOME`과 `CODEX_ELECTRON_USER_DATA_PATH`로 실행합니다. ChatGPT 26.901.20858의 별도 프로필 실행과 내장 Codex의 인증 인식을 검증했습니다. 이 환경변수는 공식 안정 API로 보장되지 않으므로 앱 업데이트 후 새 창의 프로필 메뉴에서 계정을 확인하십시오.

## 오류가 나면

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

저장 위치: `~/.local/share/codex-swap/`. 등록 정보는 `accounts.json`, 추가 계정은 `profiles/`, OpenClaw 변경 백업은 `backups/openclaw/`입니다. 테스트·분리 설치에는 `CODEX_SWAP_HOME`을 지정할 수 있습니다.

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

`XSWAP_TEST_RESERVE=1`로 실제 Codex 서버/CLI 회귀 검사를 실행하면, 모의 weekly 10% 알림 후
한도 오류 없이 다음 계정이 다음 사용자 턴을 처리하는지 확인합니다.


라이선스: [MIT](LICENSE). 보안 및 개인정보: [SECURITY.md](SECURITY.md). 기여 안내: [CONTRIBUTING.md](CONTRIBUTING.md). OpenAI의 공식 제품이 아니며, Codex·ChatGPT·OpenClaw의 라이선스는 각 제공자에게 있습니다.

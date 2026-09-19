# 예전 버전 릴리스 노트 (0.3.1 / 0.3.2)

이 문서는 `README.ko.md`에 남아 있던 버전 태그가 붙은 두 절을 그대로 옮긴 것입니다. 현재 영문 README에는 대응하는 절이 없습니다 — 두 절 모두 당시 배포된 특정 버전(0.3.1, 0.3.2)의 동작을 설명하는 릴리스 노트 성격이라 참고용으로만 남깁니다.

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


# UI / Tracked dist Rule

## Rule

`ui/src/**` 또는 UI asset을 변경하면 `cd ui && npm run build`를 실행하고 갱신된
`ui/dist/**`를 같은 변경에 포함한다. 설비 browser Firefox 60 호환과 offline asset 규칙을
유지한다.

## Constraints

- React 19 + Vite 8 + plain JSX
- routes와 API surface는 `ui/AGENTS.md` 기준
- dynamic import 등 Firefox 60 비호환 기능을 도입하지 않는다
- Lexend/JetBrains Mono local asset을 사용하고 CDN을 쓰지 않는다
- 페이지별 팔레트는 허용하지만 font/base token source는 `tokens.css`다

## Executable enforcement

- `ui-dist-fresh`: source 변경에 dist 변경이 없으면 FAIL
- `smoke`: backend/API route smoke test
- UI 자체 검증: `cd ui && npm run lint`, `cd ui && npm run build`

`ui-dist-fresh`는 build 성공 자체를 증명하지 않으므로 `/work` evidence에 실제 build 명령을
포함한다.

# Capability packs

`packs/<pack_id>/` 는 서명 전 pack 소스다. 정본 규격은 `design-reference/design/13_CAPABILITY_PACKS.md` §2 와
`contracts/schemas/capability-pack.schema.json` 이다.

```text
packs/<pack_id>/
├── PACK.json                 # 서명 없는 manifest. pack_id 는 디렉터리 이름과 같다
├── skills/                   # host-facing skill 원문 (선택)
├── context/                  # 승인된 context excerpt (선택)
├── verifiers/definitions/    # verifier profile 정의 (선택)
├── eval/cases/               # eval case (선택)
└── templates/                # 생성 템플릿 (선택)
```

- 선택 디렉터리의 부재는 오류가 아니다 (T-093). `PACK.json` 부재, symlink, 위 layout 밖의 파일은 오류다.
- `owned_paths` 를 생략하면 catalog 가 실제 파일 목록으로 채운다. 적으면 실제 파일과 정확히 같아야 한다.
- 실행 파일(`#!`, `.sh/.py/.js/...`)은 release attestation 에 digest 가 있어야 registry 가 받는다.
- `permissions` 는 요청일 뿐 grant 가 아니다. ceiling 을 넘는 요청은 설치 전 거절된다.
- `entry_capabilities` 는 정책 registry 에 등록된 adapter 이름이어야 한다. 경로 문자열로 도구를 등록할 수 없다.

봉인: `PackCatalog(root).seal(pack_id, key_id=..., private_key=...)` → `PackRegistry.register(actor, envelope)`.

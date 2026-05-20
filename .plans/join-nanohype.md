# Join nanohype org

Tactical plan for moving `stxkxs/kx` → `nanohype/kx`.

Master plan: `/Users/bs/.claude/plans/so-i-want-to-snazzy-sun.md` Phase 1.5.

## Transfer

```sh
gh repo transfer stxkxs/kx nanohype
git remote set-url origin git@github.com:nanohype/kx.git
```

## Cross-references to fix

```sh
grep -rn "stxkxs" --include="*.md" --include="*.yaml" --include="*.sh"
```

Known references:

- `CLAUDE.md:68-69` — "Related repos" section links `stxkxs/landing-zone` and `stxkxs/eks-gitops`
- `CLAUDE.md:5` — links `eks-gitops`
- The Druid post-renderer that consumes `eks-gitops/catalog/druid/chart/` — if the post-renderer fetches via URL, update; if it reads from a local sibling checkout, no change needed
- `stack/data/druid/install.sh` — verify how it references the upstream chart

## Notes

- This is a personal cluster repo — no CI, no OIDC, no registries to update
- Most file URLs are upstream Helm chart sources (cilium, cert-manager, etc.) — no org coupling
- `install.sh` files pin chart versions explicitly; those don't change with the transfer

## Verification

```sh
gh repo view nanohype/kx                                               # 200
task up                                                                # cluster comes up clean
task status                                                            # core stack healthy
grep -rn "stxkxs" --include="*.md" --include="*.yaml" --include="*.sh" # zero or intentional
```

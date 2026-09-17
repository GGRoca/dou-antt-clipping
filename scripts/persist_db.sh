#!/usr/bin/env bash
# Grava data/clipping.sqlite no branch 'data' usando um worktree separado
# (nao mexe no checkout principal). Idempotente: sem mudanca, sem commit
# e sem acesso a rede.
set -euo pipefail
DB=data/clipping.sqlite
if [ ! -f "$DB" ]; then
  echo "Sem $DB para persistir"; exit 0
fi
if [ -f "$DB.restored" ] && cmp -s "$DB" "$DB.restored"; then
  echo "Banco sem alteracoes (identico ao restaurado)"; exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

rc=1
for i in 1 2 3; do
  set +e
  out=$(git ls-remote --exit-code --heads origin data 2>&1)
  rc=$?
  set -e
  if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then break; fi
  echo "git ls-remote falhou (rc=$rc), tentativa $i/3: $out"
  sleep $((i * 5))
done
WT=$(mktemp -d)
case $rc in
  0) git fetch --quiet origin data
     git worktree add --quiet --detach "$WT" FETCH_HEAD ;;
  2) git worktree add --quiet --detach "$WT"
     git -C "$WT" checkout --quiet --orphan data
     git -C "$WT" rm -rfq . 2>/dev/null || true ;;
  *) echo "Nao foi possivel consultar o remoto apos 3 tentativas"; exit 1 ;;
esac

mkdir -p "$WT/data"
cp "$DB" "$WT/$DB"
cd "$WT"
git add -f "$DB"
if git diff --cached --quiet; then
  echo "Banco sem alteracoes"; exit 0
fi
MSG="Update database [$(date -u +%Y-%m-%dT%H:%M:%SZ)]"
git commit --quiet -m "$MSG"

for i in 1 2 3; do
  if git push --quiet origin HEAD:data; then
    echo "Banco persistido no branch 'data'"; exit 0
  fi
  echo "push falhou (tentativa $i) -- reaplicando em cima do remoto"
  sleep $((i * 5))
  git fetch --quiet origin data
  git reset --quiet --soft FETCH_HEAD
  git add -f "$DB"
  git commit --quiet -m "$MSG" || true
done
echo "Nao foi possivel persistir o banco"; exit 1

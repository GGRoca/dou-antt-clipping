#!/usr/bin/env bash
# Restaura data/clipping.sqlite a partir do branch 'data'.
# Branch inexistente = banco novo. Falha de rede = aborta (rodar sem estado
# reenviaria e-mails e sobrescreveria o historico).
set -euo pipefail
DB=data/clipping.sqlite
mkdir -p data

set +e
git ls-remote --exit-code --heads origin data >/dev/null 2>&1
rc=$?
set -e
case $rc in
  0) ;;
  2) echo "Branch 'data' inexistente -- banco novo"; exit 0 ;;
  *) echo "Nao foi possivel consultar o remoto (git ls-remote rc=$rc)"; exit 1 ;;
esac

git fetch --quiet origin data
if git show FETCH_HEAD:"$DB" > "$DB" 2>/dev/null; then
  echo "Banco restaurado: $(stat -c%s "$DB") bytes"
else
  rm -f "$DB"
  echo "Branch 'data' existe mas sem $DB -- banco novo"
fi

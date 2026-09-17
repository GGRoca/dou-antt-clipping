#!/usr/bin/env bash
# Restaura data/clipping.sqlite a partir do branch 'data'.
# Branch inexistente = banco novo. Falha de rede = aborta (rodar sem estado
# reenviaria e-mails e sobrescreveria o historico).
set -euo pipefail
DB=data/clipping.sqlite
mkdir -p data

# git ls-remote --exit-code: 0 = branch existe, 2 = nao existe, outro = erro (rede, auth)
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
case $rc in
  0) ;;
  2) echo "Branch 'data' inexistente -- banco novo"; exit 0 ;;
  *) echo "Nao foi possivel consultar o remoto apos 3 tentativas"; exit 1 ;;
esac

git fetch --quiet origin data
if git show FETCH_HEAD:"$DB" > "$DB" 2>/dev/null; then
  cp "$DB" "$DB.restored"   # referencia para o persist saber se algo mudou
  echo "Banco restaurado: $(stat -c%s "$DB") bytes"
else
  rm -f "$DB" "$DB.restored"
  echo "Branch 'data' existe mas sem $DB -- banco novo"
fi

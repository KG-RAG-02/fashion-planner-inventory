#!/usr/bin/env bash
# docs/diagrams/src/*.dot 전부를 라이트·다크 SVG와 PNG로 만든다.
#   docs/diagrams/<name>.svg, <name>-dark.svg, <name>.png, <name>-dark.png
# 필요: Graphviz(dot), librsvg(rsvg-convert), Pretendard 폰트(OFL, 로컬 설치). 폰트 파일은 커밋하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
src=docs/diagrams/src
out=docs/diagrams

render() {  # $1 = .dot 파일, $2 = 접미사("" 또는 "-dark"), 그 뒤는 토큰=값 쌍
  local dot_file=$1 suffix=$2; shift 2
  local name; name=$(basename "$dot_file" .dot)
  local sed_args=()
  for pair in "$@"; do sed_args+=(-e "s|@${pair%%=*}@|${pair#*=}|g"); done
  sed "${sed_args[@]}" "$dot_file" | dot -Tsvg -o "$out/$name$suffix.svg"
  rsvg-convert -w 1600 "$out/$name$suffix.svg" -o "$out/$name$suffix.png"
}

for f in "$src"/*.dot; do
  render "$f" ""      BG='#ffffff' FG='#1f2328' SVC_FILL='#eef2ff' SVC_LINE='#4f46e5' EXT='#6b7280' ERR='#b91c1c'
  render "$f" "-dark" BG='#0d1117' FG='#e6edf3' SVC_FILL='#1e1b4b' SVC_LINE='#818cf8' EXT='#6b7280' ERR='#f87171'
done
echo "rendered: $(ls "$out"/*.svg | wc -l | tr -d ' ') svg"

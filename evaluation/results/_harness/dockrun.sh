#!/bin/sh
# Launch one evaluation process in a THROWAWAY container built from the app image.
# Never touches the serving containers (uk-rent-app :5001, uk-rent-app-fc :5002).
# Usage: dockrun.sh <container-name> <python-args...>
set -eu
NAME="$1"; shift
IMAGE="${EVAL_IMAGE:-uk-rent-agent:canary-fc-loop-0952c56}"
exec docker run --rm --name "$NAME" \
  --network uk_rent_recommendation_rentnet \
  -v /home/shuhan/uk_rent_recommendation:/work -w /work \
  --user "$(id -u):$(id -g)" \
  -v "${EVAL_CACHE_DIR:-/tmp/rc_eval_cache}":/evalcache \
  -e HOME=/evalcache -e HF_HOME=/evalcache/hf -e TZ=Europe/London \
  -e PYTHONUNBUFFERED=1 -e PYTHONIOENCODING=utf-8 \
  -e AGENT_ARCH=fc_loop \
  -e SEARXNG_URL=http://searxng:8080 \
  --entrypoint python "$IMAGE" "$@"

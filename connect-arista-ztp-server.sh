#!/bin/bash
# Open a shell inside the container.
#
# Thin wrapper around start.sh, which decides on its own whether the Docker
# daemon needs sudo. These helpers used to hardcode 'sudo docker', so they
# and ./start.sh disagreed about how to reach Docker on the same host.
exec "$(dirname "$(readlink -f "$0")")/start.sh" shell "$@"

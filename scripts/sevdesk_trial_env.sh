# Sourced by the sevDesk trial recipes of the justfile, with the repository root as its argument. It refuses a run
# whose settings could mix with the shell's or land in the repository, and sets `env_file` to the trial's env file:
# - any SEVDESK__ variable of the shell, in any letter case (pydantic-settings reads names case-insensitively, and
#   the shell's variables take precedence over the file);
# - a path with whitespace, which `uv run --env-file` would split;
# - a file inside the repository, where it could be committed.
# Whether the file exists is the recipe's call.

# grep reads all of it (no -q): an early exit could fail the pipe under pipefail and let the check pass.
if compgen -e | grep -i '^SEVDESK__' >/dev/null; then
    echo "unset the SEVDESK__ variables of this shell first" >&2
    exit 1
fi
env_file="${SKILLFORGE_SEVDESK_TRIAL_ENV:-$HOME/.config/skillforge/sevdesk-trial.env}"
case "$env_file" in
    *[[:space:]]*) echo "the trial env file's path must not contain whitespace" >&2; exit 1 ;;
esac
repository="$(cd -- "$1" && pwd -P)"
if env_dir="$(cd -- "$(dirname -- "$env_file")" 2>/dev/null && pwd -P)"; then
    case "$env_dir/" in
        "$repository"/*) echo "the trial env file must live outside the repository" >&2; exit 1 ;;
    esac
fi

# termpilot.zsh: inline command suggestions from your history (zsh >= 5.3).
#
# Load it from ~/.zshrc; `termpilot init zsh` prints the exact line. As you
# type, the best match from your history appears as dimmed "ghost text"
# after the cursor.
#
#   Right arrow, End, Ctrl-E   (cursor at end of line) accept the suggestion
#                              into the command line. It is NOT run: press
#                              Enter yourself, as always.
#   termpilot_disable          turn suggestions off in this shell
#   termpilot_enable           turn them back on
#
# Settings (set before sourcing this file):
#   TERMPILOT_BIN         command to run          (default: termpilot)
#   TERMPILOT_HIGHLIGHT   ghost text style        (default: fg=8)
#   TERMPILOT_CONTEXT     log cwd per command     (default: 1, 0 to disable)
#   TERMPILOT_AUTO_INDEX  refresh after commands  (default: 1, 0 to disable)
#
# Safety: the typed text reaches `termpilot` as one argv element, and a
# suggestion is only ever placed in POSTDISPLAY (display only) or, when you
# accept it, in BUFFER. Nothing in this file evals, runs, or expands
# suggestion or history text.

[[ -o interactive ]] || return 0

autoload -Uz is-at-least add-zle-hook-widget add-zsh-hook
if ! is-at-least 5.3; then
  print -u2 "termpilot: zsh 5.3 or newer is required (this is $ZSH_VERSION)"
  return 1
fi
(( ${+_termpilot_loaded} )) && return 0

: ${TERMPILOT_BIN:=termpilot}
: ${TERMPILOT_HIGHLIGHT:=fg=8}
: ${TERMPILOT_CONTEXT:=1}
: ${TERMPILOT_AUTO_INDEX:=1}

if ! (( ${+commands[$TERMPILOT_BIN]} )) && [[ ! -x $TERMPILOT_BIN ]]; then
  print -u2 "termpilot: '$TERMPILOT_BIN' not found; install TermPilot or set TERMPILOT_BIN"
  return 1
fi
typeset -g _termpilot_loaded=1

zmodload -F zsh/system p:sysparams 2>/dev/null     # to cancel stale requests
zmodload -F zsh/datetime p:EPOCHSECONDS 2>/dev/null

typeset -g _termpilot_suggestion=''   # full command currently suggested
typeset -g _termpilot_hl=''           # the region_highlight entry we added
typeset -g _termpilot_last_buffer=''  # BUFFER at the last redraw
typeset -g _termpilot_request=''      # BUFFER the in-flight request is for
typeset -g _termpilot_fd='' _termpilot_pid=''
typeset -g _termpilot_log=''          # context log path ('' = disabled)
typeset -g -i _termpilot_ran=0 _termpilot_enabled=0
typeset -ga _termpilot_wrapped=(forward-char end-of-line vi-forward-char vi-end-of-line)

# --- Display -----------------------------------------------------------------

_termpilot_clear() {
  if [[ -n $_termpilot_hl ]]; then
    region_highlight=( "${(@)region_highlight:#${(b)_termpilot_hl}}" )
    _termpilot_hl=''
  fi
  [[ -n $_termpilot_suggestion ]] && POSTDISPLAY=''
  _termpilot_suggestion=''
}

# Show $1 as ghost text: the untyped remainder if it extends BUFFER,
# otherwise (a fuzzy match) the whole command after an arrow.
_termpilot_display() {
  _termpilot_clear
  local s=$1
  [[ -n $s && $s != "$BUFFER" ]] || return 0
  if [[ $s == "$BUFFER"* ]]; then
    POSTDISPLAY=${s#"$BUFFER"}
  else
    POSTDISPLAY="  → $s"
  fi
  _termpilot_suggestion=$s
  _termpilot_hl="$#BUFFER $(( $#BUFFER + $#POSTDISPLAY )) $TERMPILOT_HIGHLIGHT"
  region_highlight+=( "$_termpilot_hl" )
}

# --- Asynchronous fetching -------------------------------------------------------

_termpilot_cancel() {
  if [[ -n $_termpilot_fd ]]; then
    zle -F $_termpilot_fd 2>/dev/null
    exec {_termpilot_fd}<&-
    _termpilot_fd=''
  fi
  if [[ -n $_termpilot_pid ]]; then
    kill -TERM $_termpilot_pid 2>/dev/null
    _termpilot_pid=''
  fi
}

_termpilot_fetch() {
  _termpilot_cancel
  _termpilot_request=$BUFFER
  exec {_termpilot_fd}< <(
    "$TERMPILOT_BIN" suggest --format inline -- "$_termpilot_request" </dev/null 2>/dev/null
  )
  (( ${+sysparams} )) && _termpilot_pid=$sysparams[procsubstpid]
  zle -F $_termpilot_fd _termpilot_response
}

# Called by zle when the request's output is ready (or the pipe closed).
_termpilot_response() {
  emulate -L zsh
  local fd=$1 suggestion=''
  IFS='' read -r -d '' -u $fd suggestion 2>/dev/null
  zle -F $fd 2>/dev/null
  exec {fd}<&-
  if [[ $fd == $_termpilot_fd ]]; then
    _termpilot_fd='' _termpilot_pid=''
  fi
  zle _termpilot-show -- "$suggestion"
}

_termpilot_show() {
  # Drop stale answers: the line changed since the request was made.
  [[ $BUFFER == "$_termpilot_request" ]] && (( CURSOR == $#BUFFER )) || return 0
  _termpilot_display "$1"
  zle -R
}

# --- Reacting to edits --------------------------------------------------------

_termpilot_pre_redraw() {
  if [[ $BUFFER == "$_termpilot_last_buffer" ]]; then
    # Cursor moved off the end: ghost text would be misleading there.
    (( CURSOR != $#BUFFER )) && [[ -n $_termpilot_suggestion ]] && _termpilot_clear
    return 0
  fi
  _termpilot_last_buffer=$BUFFER
  local previous=$_termpilot_suggestion
  _termpilot_clear
  _termpilot_cancel
  case $LASTWIDGET in
    # A line recalled from history is already complete; don't decorate it.
    (*history*|*search*) return 0 ;;
  esac
  [[ -n ${BUFFER//[[:space:]]/} ]] && (( CURSOR == $#BUFFER )) || return 0
  # Instant feedback while the new request runs: keep the previous
  # suggestion if what was typed is still a prefix of it.
  if [[ -n $previous && $previous == "$BUFFER"* ]]; then
    _termpilot_display "$previous"
  fi
  _termpilot_fetch
}

_termpilot_line_init() {
  _termpilot_last_buffer='' _termpilot_suggestion='' _termpilot_hl=''
}

_termpilot_line_finish() {
  # Remove the ghost text so it never lingers in the scrollback.
  _termpilot_cancel
  _termpilot_clear
  zle -R
}

# --- Accepting ------------------------------------------------------------------

# Put the suggestion in the command line. Never executes anything.
_termpilot_accept() {
  [[ -n $_termpilot_suggestion ]] && (( CURSOR == $#BUFFER )) || return 1
  local s=$_termpilot_suggestion
  _termpilot_cancel
  _termpilot_clear
  BUFFER=$s
  CURSOR=$#BUFFER
  _termpilot_last_buffer=$BUFFER
}

# Replaces forward-char, end-of-line, ...: accept if a suggestion is shown,
# otherwise behave exactly like the widget it replaced.
_termpilot_accept_or_original() {
  _termpilot_accept || zle termpilot-orig-$WIDGET -- "$@"
}

_termpilot_accept_widget() {
  _termpilot_accept
}

# --- Context log and background index refresh -------------------------------

_termpilot_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//$'\n'/\\n}
  s=${s//$'\t'/\\t}
  REPLY=$s
}

_termpilot_preexec() {
  _termpilot_ran=1
  [[ $TERMPILOT_CONTEXT == 1 && -n $_termpilot_log ]] || return 0
  [[ -n ${1//[[:space:]]/} ]] || return 0
  # Respect HIST_IGNORE_SPACE: a leading space means "don't remember this".
  [[ -o hist_ignore_space && $1 == [[:space:]]* ]] && return 0
  local cmd dir
  _termpilot_escape "$1"; cmd=$REPLY
  _termpilot_escape "$PWD"; dir=$REPLY
  if [[ ! -e $_termpilot_log ]]; then
    ( umask 077 && : >> "$_termpilot_log" ) 2>/dev/null
  fi
  print -r -- "$EPOCHSECONDS"$'\t'"$dir"$'\t'"$cmd" >> "$_termpilot_log" 2>/dev/null
}

_termpilot_precmd() {
  (( _termpilot_ran )) || return 0
  _termpilot_ran=0
  [[ $TERMPILOT_AUTO_INDEX == 1 ]] || return 0
  # Detached and silent: never delays the prompt. HISTFILE is passed via the
  # environment because zsh does not export it by default.
  HISTFILE=$HISTFILE "$TERMPILOT_BIN" index --quiet </dev/null >/dev/null 2>&1 &!
}

# --- Enable / disable -----------------------------------------------------------

termpilot_enable() {
  (( _termpilot_enabled )) && return 0
  local w
  for w in $_termpilot_wrapped; do
    # Keep whatever the widget did before (builtin or another plugin's).
    if [[ $widgets[$w] == user:* ]]; then
      zle -A $w termpilot-orig-$w
    else
      zle -A .$w termpilot-orig-$w
    fi
    zle -N $w _termpilot_accept_or_original
  done
  zle -N termpilot-accept _termpilot_accept_widget
  zle -N _termpilot-show _termpilot_show
  add-zle-hook-widget line-pre-redraw _termpilot_pre_redraw
  add-zle-hook-widget line-init _termpilot_line_init
  add-zle-hook-widget line-finish _termpilot_line_finish
  add-zsh-hook preexec _termpilot_preexec
  add-zsh-hook precmd _termpilot_precmd
  _termpilot_enabled=1
}

termpilot_disable() {
  (( _termpilot_enabled )) || return 0
  _termpilot_cancel
  local w
  for w in $_termpilot_wrapped; do
    zle -A termpilot-orig-$w $w
  done
  add-zle-hook-widget -d line-pre-redraw _termpilot_pre_redraw
  add-zle-hook-widget -d line-init _termpilot_line_init
  add-zle-hook-widget -d line-finish _termpilot_line_finish
  add-zsh-hook -d preexec _termpilot_preexec
  add-zsh-hook -d precmd _termpilot_precmd
  _termpilot_enabled=0
}

# Ask TermPilot (once, at startup) where the context log lives so the
# preexec hook can append to it without starting Python per command.
() {
  local line
  for line in "${(@f)$("$TERMPILOT_BIN" paths </dev/null 2>/dev/null)}"; do
    [[ $line == context_log=* ]] && _termpilot_log=${line#context_log=}
  done
}

termpilot_enable

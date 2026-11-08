// PreToolUse hook for the Bash and Monitor tools: stops merges, secret reads,
// remote shells and repository administration before they run. Agents act
// with the maintainer's logins, so neither GitHub nor this machine can tell
// them apart. The hook catches accidents and is not a security boundary: a
// command that computes its target at run time, or a program that reads a
// secret on its own, gets through.
//
// Claude Code passes the tool call as JSON on stdin. The hook splits the
// command line into simple commands the way a POSIX shell does (quotes,
// escapes, operators, subshells, command substitutions, heredocs), unwraps
// wrappers such as env, command, xargs, find -exec, and sh -c, follows cd and
// plain variable assignments, and checks every command it finds. A blocked
// call exits with code 2 and a message on stderr, which Claude Code shows to
// Claude as the reason; anything else exits 0 and the normal permission flow
// applies. A command the guard fails to check is blocked.
//
// Only node: built-ins and no network, so it stays fast: a hook that times out
// does not block. skillforge and skillsite carry the same file and tests;
// change both together.

import { readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// GraphQL mutations agents may run. Every other mutation is blocked.
const GRAPHQL_MUTATIONS = [
  "setIssueFieldValue",
  "updateProjectV2ItemFieldValue",
];

// REST writes agents may run, as [method, path without the leading slash]:
// issue comments and the sub-issue endpoints (add, reprioritise, remove).
const REST_WRITES = [
  ["POST", /^repos\/[^/]+\/[^/]+\/issues\/[^/]+\/comments$/],
  ["POST", /^repos\/[^/]+\/[^/]+\/issues\/[^/]+\/sub_issues$/],
  ["PATCH", /^repos\/[^/]+\/[^/]+\/issues\/[^/]+\/sub_issues\/priority$/],
  ["DELETE", /^repos\/[^/]+\/[^/]+\/issues\/[^/]+\/sub_issue$/],
];

const ASK =
  "Stop and ask the maintainer instead. The guard prevents accidents and is not a security boundary; never work around it.";

export const RULES = {
  "pr-merge": "Only the maintainer merges pull requests.",
  "stack-merge": "Only the maintainer merges pull requests, stacks included.",
  secrets:
    "Agents never read, print, copy or source secrets: .env files other than *.example, ~/.config/skillforge/, the environment, secret variables and tokens. Find env files with ls or git check-ignore, never by reading them.",
  "remote-shell":
    "Agents open no remote shells and copy nothing to or from other hosts (ssh, scp, sftp, ssh-add, rsync to a host).",
  "repo-admin":
    "Secrets, variables, rulesets, releases, tags and repository settings are left to the maintainer.",
  "repo-destructive":
    "Deleting, archiving, renaming, or transferring the repository and changing its visibility are left to the maintainer.",
  "issue-destructive":
    "Deleting or transferring issues is left to the maintainer.",
  "project-destructive":
    "Deleting projects or project fields is left to the maintainer.",
  "label-delete": "Deleting labels is left to the maintainer.",
  "gh-alias":
    "A gh alias could hide a blocked command from the guard; aliases are left to the maintainer.",
  "api-write": `gh api may only read (GET), plus these writes: issue comments, sub-issues, and the GraphQL mutations ${GRAPHQL_MUTATIONS.join(" and ")}.`,
  "api-unreadable":
    "The guard cannot read the GraphQL document, so it cannot tell a query from a mutation. Pass the document inline with -f query='…' (or -F query=@file with a file it can read), or ask the maintainer.",
  "guard-error":
    "The guard could not check this command and blocks it. Write the command more simply, or ask the maintainer.",
};

const SELF_EXPLAINING = new Set(["api-unreadable", "guard-error"]);

export function message({ rule, command, detail }) {
  const shown = command.length > 200 ? `${command.slice(0, 200)}…` : command;
  const lines = [
    `Blocked by .claude/hooks/guard.mjs, rule "${rule}": ${shown}`,
  ];
  if (detail) lines.push(`Found: ${detail}.`);
  lines.push(RULES[rule]);
  if (!SELF_EXPLAINING.has(rule)) lines.push(ASK);
  return lines.join("\n");
}

// --- Shell ------------------------------------------------------------------

const REDIRECTIONS = [
  "<<<",
  "<<-",
  "<<",
  "<>",
  "<&",
  "<",
  "&>>",
  "&>",
  ">>",
  ">|",
  ">&",
  ">",
];
const OPERATORS = [";;&", ";;", ";&", ";", "&&", "&", "||", "|&", "|"];
const NAME = /^[A-Za-z_][A-Za-z0-9_]*/;

// Splits shell source into simple commands. Each command is
// { words, redirects, stdin, next }, where next is the command a pipe feeds. A word is { text, dynamic, quoted, vars }, where
// text has quotes and escapes removed, dynamic marks a word with an expansion
// ($x, $(…), `…`), quoted one with quotes or escapes, and vars lists the
// variables it expands. redirects holds the targets of <, >, >> and the like
// as words with an op; stdin is { text, expands } for a heredoc or
// here-string or { file } for "< file". Commands inside substitutions,
// subshells, and unquoted heredocs are listed too. With nested set, lexing
// stops at the ")" that closes a "$(" and returns the index after it in end.
function parseShell(src, start = 0, nested = false) {
  const commands = [];
  const newCommand = () => ({ words: [], redirects: [], stdin: null });
  let current = newCommand();
  let word = null;
  let redirect = null;
  let heredocs = [];
  let pipeFrom = null;
  let depth = 0;
  let i = start;

  const append = (
    text,
    { dynamic = false, quoted = false, variable = null } = {},
  ) => {
    word ??= { text: "", dynamic: false, quoted: false, vars: [] };
    word.text += text;
    if (dynamic) word.dynamic = true;
    if (quoted) word.quoted = true;
    if (variable !== null) word.vars.push(variable);
  };
  const endWord = () => {
    if (word === null) return;
    if (redirect === null) current.words.push(word);
    else if (redirect === "<<" || redirect === "<<-")
      heredocs.push({
        delimiter: word.text,
        strip: redirect === "<<-",
        // Only a heredoc with an unquoted delimiter expands $x, $(…) and `…`.
        expands: !word.quoted,
        command: current,
      });
    else {
      current.redirects.push({ ...word, op: redirect });
      if (redirect === "<") current.stdin = { file: word.text };
      else if (redirect === "<<<")
        current.stdin = { text: `${word.text}\n`, expands: false };
    }
    redirect = null;
    word = null;
  };
  const endCommand = () => {
    endWord();
    if (current.words.length > 0 || current.redirects.length > 0) {
      commands.push(current);
      // A pipe links its producer to the command it feeds.
      if (pipeFrom !== null && pipeFrom !== current) {
        pipeFrom.next = current;
        pipeFrom = null;
      }
    }
    current = newCommand();
  };
  const readHeredocs = () => {
    for (const heredoc of heredocs) {
      let body = "";
      while (i < src.length) {
        const newline = src.indexOf("\n", i);
        const end = newline === -1 ? src.length : newline;
        const line = src.slice(i, end);
        i = end + 1;
        const compared = heredoc.strip ? line.replace(/^\t+/, "") : line;
        if (compared === heredoc.delimiter) break;
        body += `${line}\n`;
      }
      heredoc.command.stdin = { text: body, expands: heredoc.expands };
      if (heredoc.expands) commands.push(...substitutionsIn(body));
    }
    heredocs = [];
  };
  const substitution = (from) => {
    const inner = parseShell(src, from, true);
    commands.push(...inner.commands);
    return inner.end;
  };
  // Handles a "$" at i and returns the index after the expansion.
  const dollar = () => {
    if (src.startsWith("$((", i)) {
      let level = 0;
      let j = i + 1;
      for (; j < src.length; j++) {
        if (src[j] === "(") level++;
        else if (src[j] === ")" && --level === 0) break;
      }
      commands.push(...substitutionsIn(src.slice(i + 3, j - 1)));
      append(src.slice(i, j + 1), DYNAMIC);
      return j + 1;
    }
    if (src[i + 1] === "(") {
      const end = substitution(i + 2);
      append(src.slice(i, end), DYNAMIC);
      return end;
    }
    if (src[i + 1] === "{") {
      const close = src.indexOf("}", i);
      const end = close === -1 ? src.length : close + 1;
      const inner = src.slice(i + 2, end - 1);
      const name = NAME.exec(inner.replace(/^[#!]/, ""));
      // ${x:-$(cmd)} runs cmd.
      commands.push(...substitutionsIn(inner));
      append(src.slice(i, end), { dynamic: true, variable: name?.[0] ?? null });
      return end;
    }
    const name = /^(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])/.exec(
      src.slice(i + 1),
    );
    if (name === null) {
      append("$");
      return i + 1;
    }
    append(`$${name[0]}`, { dynamic: true, variable: name[0] });
    return i + 1 + name[0].length;
  };
  const backtick = () => {
    const { inner, end } = backquoted(src, i);
    commands.push(...parseShell(inner).commands);
    append(src.slice(i, end), DYNAMIC);
    return end;
  };

  while (i < src.length) {
    const c = src[i];
    if (c === "\\") {
      if (src[i + 1] !== "\n") append(src[i + 1] ?? "", QUOTED);
      i += 2;
    } else if (c === "'") {
      const close = src.indexOf("'", i + 1);
      const end = close === -1 ? src.length : close;
      append(src.slice(i + 1, end), QUOTED);
      i = end + 1;
    } else if (c === "$" && src[i + 1] === "'") {
      const ansi = ansiC(src, i + 2);
      append(ansi.text, QUOTED);
      i = ansi.end;
    } else if (c === '"' || (c === "$" && src[i + 1] === '"')) {
      i += c === "$" ? 2 : 1;
      append("", QUOTED);
      while (i < src.length && src[i] !== '"') {
        if (src[i] === "\\" && '$`"\\\n'.includes(src[i + 1])) {
          if (src[i + 1] !== "\n") append(src[i + 1]);
          i += 2;
        } else if (src[i] === "$") i = dollar();
        else if (src[i] === "`") i = backtick();
        else append(src[i++]);
      }
      i++;
    } else if (c === "$") {
      i = dollar();
    } else if (c === "`") {
      i = backtick();
    } else if (c === "#" && word === null) {
      while (i < src.length && src[i] !== "\n") i++;
    } else if (c === " " || c === "\t" || c === "\r") {
      endWord();
      i++;
    } else if (c === "\n") {
      endCommand();
      i++;
      readHeredocs();
    } else if ((c === "<" || c === ">") && src[i + 1] === "(") {
      const end = substitution(i + 2);
      append(src.slice(i, end), DYNAMIC);
      i = end;
    } else if (REDIRECTIONS.some((op) => src.startsWith(op, i))) {
      const op = REDIRECTIONS.find((candidate) => src.startsWith(candidate, i));
      // A number right before the operator is a file descriptor (2>&1).
      if (word !== null && !word.quoted && /^\d+$/.test(word.text)) word = null;
      else endWord();
      redirect = op;
      i += op.length;
    } else if (c === "(") {
      endCommand();
      depth++;
      i++;
    } else if (c === ")") {
      endCommand();
      i++;
      if (depth === 0 && nested) return { commands, end: i };
      depth = Math.max(0, depth - 1);
    } else if (OPERATORS.some((op) => src.startsWith(op, i))) {
      const op = OPERATORS.find((candidate) => src.startsWith(candidate, i));
      const producer = current;
      endCommand();
      if (op === "|" || op === "|&") pipeFrom = producer;
      i += op.length;
    } else {
      append(c);
      i++;
    }
  }
  endCommand();
  return { commands, end: i };
}

const DYNAMIC = { dynamic: true };
const QUOTED = { quoted: true };

// Returns the source inside a `…` substitution that starts at i, with its
// escapes removed, and the index after the closing backquote.
function backquoted(src, i) {
  let j = i + 1;
  let inner = "";
  for (; j < src.length && src[j] !== "`"; j++) {
    if (src[j] === "\\" && "`$\\".includes(src[j + 1])) j++;
    inner += src[j];
  }
  return { inner, end: j + 1 };
}

// Returns the commands of the $(…) and `…` substitutions in the body of a
// heredoc whose delimiter is unquoted.
function substitutionsIn(text) {
  const commands = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\\") i++;
    else if (text.startsWith("$(", i) && text[i + 2] !== "(") {
      const inner = parseShell(text, i + 2, true);
      commands.push(...inner.commands);
      i = inner.end - 1;
    } else if (text[i] === "`") {
      const { inner, end } = backquoted(text, i);
      commands.push(...parseShell(inner).commands);
      i = end - 1;
    }
  }
  return commands;
}

// Decodes $'…' (ANSI-C quoting) from start, just after the opening quote.
function ansiC(src, start) {
  const escapes = { n: "\n", t: "\t", r: "\r", a: "\x07", b: "\b" };
  let text = "";
  let i = start;
  while (i < src.length && src[i] !== "'") {
    if (src[i] !== "\\") {
      text += src[i++];
      continue;
    }
    const next = src[i + 1] ?? "";
    const hex = /^x([0-9A-Fa-f]{1,2})/.exec(src.slice(i + 1));
    const octal = /^[0-7]{1,3}/.exec(src.slice(i + 1));
    if (hex) {
      text += String.fromCharCode(parseInt(hex[1], 16));
      i += 1 + hex[0].length;
    } else if (octal) {
      text += String.fromCharCode(parseInt(octal[0], 8));
      i += 1 + octal[0].length;
    } else {
      text += escapes[next] ?? next;
      i += 2;
    }
  }
  return { text, end: i + 1 };
}

const RESERVED = new Set([
  "!",
  "{",
  "}",
  "if",
  "then",
  "else",
  "elif",
  "fi",
  "do",
  "done",
  "while",
  "until",
  "esac",
]);
// Words after these are a loop variable or a case subject, not a command.
const NOT_A_COMMAND = new Set(["for", "select", "case"]);
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=/;
const SHELLS = new Set([
  "sh",
  "bash",
  "zsh",
  "dash",
  "ksh",
  "fish",
  "csh",
  "tcsh",
]);

// Wrappers that run their arguments as a command, with the options that take
// a value. env, command, timeout, watch, eval, find, the runners, and the
// shells are handled separately.
const WRAPPERS = {
  builtin: [],
  caffeinate: ["-t", "-w"],
  doas: ["-u", "-C"],
  exec: ["-a"],
  nice: ["-n"],
  nohup: [],
  noglob: [],
  nocorrect: [],
  stdbuf: ["-i", "-o", "-e"],
  sudo: ["-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-T", "-D", "-R"],
  time: ["-f", "-o"],
  xargs: ["-I", "-J", "-L", "-R", "-n", "-P", "-s", "-E", "-d", "-a"],
};

// Package runners that run their arguments as a command: the subcommand that
// does so (none for npx and uvx) and the options that take a value.
const RUNNERS = {
  uv: {
    subs: ["run"],
    values: [
      "--with",
      "--with-editable",
      "--with-requirements",
      "-p",
      "--python",
      "--env-file",
      "--package",
      "--directory",
      "--project",
      "--extra",
      "--group",
      "--only-group",
      "--no-group",
      "--index",
      "--default-index",
      "-w",
    ],
  },
  uvx: {
    subs: null,
    values: ["--from", "--with", "-p", "--python", "--index"],
  },
  pnpm: {
    subs: ["exec", "dlx"],
    values: ["--filter", "-F", "-C", "--dir", "--package"],
  },
  npm: {
    subs: ["exec", "x"],
    values: ["-w", "--workspace", "-p", "--package", "-c", "--call"],
  },
  npx: { subs: null, values: ["-p", "--package", "-c", "--call"] },
  bunx: { subs: null, values: ["-p", "--package"] },
};

// Returns the command a runner runs, or null when it runs none (pnpm test).
function unwrapRunner(words, { subs, values }) {
  let needsSub = subs !== null;
  for (let i = 0; i < words.length; i++) {
    const text = words[i].text;
    if (text === "--") return needsSub ? null : words.slice(i + 1);
    if (values.includes(text)) i++;
    else if (text.startsWith("-")) continue;
    else if (needsSub) {
      if (!subs.includes(text)) return null;
      needsSub = false;
    } else return words.slice(i);
  }
  return null;
}
const FIND_ACTIONS = ["-exec", "-execdir", "-ok", "-okdir"];

const basename = (text) => text.split(/[\\/]/).pop();

function skipOptions(words, withValue) {
  let i = 0;
  while (i < words.length) {
    const text = words[i].text;
    if (text === "--") return words.slice(i + 1);
    if (!text.startsWith("-") || text === "-") break;
    i += withValue.includes(text) ? 2 : 1;
  }
  return words.slice(i);
}

// Returns the string env -S splits into words and the index after it, or null.
function splitString(words, i) {
  const text = words[i].text;
  if (text === "-S" || text === "--split-string")
    return { text: words[i + 1]?.text ?? "", next: i + 2 };
  if (text.startsWith("--split-string="))
    return { text: text.slice("--split-string=".length), next: i + 1 };
  // -S in a bundle (-vS 'cmd') or with the string attached (-S'cmd').
  const bundle = /^-[A-Za-z]*S(.*)$/s.exec(text);
  if (bundle === null) return null;
  return bundle[1] !== ""
    ? { text: bundle[1], next: i + 1 }
    : { text: words[i + 1]?.text ?? "", next: i + 2 };
}

// Returns env's own words (head) and the command it runs (inner).
function unwrapEnv(words) {
  let i = 0;
  while (i < words.length) {
    const text = words[i].text;
    const split = splitString(words, i);
    if (split !== null) {
      const inner = parseShell(split.text).commands;
      return {
        head: words.slice(0, split.next),
        inner: [
          ...inner.flatMap((command) => command.words),
          ...words.slice(split.next),
        ],
      };
    }
    if (["-u", "--unset", "-C", "--chdir", "-P"].includes(text)) i += 2;
    else if (text.startsWith("-") || ASSIGNMENT.test(text)) i++;
    else break;
  }
  return { head: words.slice(0, i), inner: words.slice(i) };
}

// Returns what a shell runs: { script } for -c (null when it is missing),
// { file: true } for a script file, { file: false } for a script on stdin.
function shellScript(words) {
  let command = false;
  let stdin = false;
  for (let i = 0; i < words.length; i++) {
    const text = words[i].text;
    if (text === "--")
      return command
        ? { script: words[i + 1] ?? null }
        : { file: !stdin && i + 1 < words.length };
    if (/^[-+][oO]$/.test(text) || ["--rcfile", "--init-file"].includes(text))
      i++;
    else if (/^-[A-Za-z]+$/.test(text)) {
      command ||= text.includes("c");
      stdin ||= text.includes("s");
    } else if (/^\+[A-Za-z]+$/.test(text) || text.startsWith("--")) continue;
    else return command ? { script: words[i] } : { file: !stdin };
  }
  return command ? { script: null } : { file: false };
}

// Expands a simple command into the invocations it runs: strips reserved
// words and assignments and unwraps wrappers. A wrapper is listed with its own
// options, so their paths are checked too. The scripts of sh -c, eval, and
// watch are parsed and pushed onto nested, as { command, level }.
function expand(command, level, nested) {
  if (level > 10) throw new Error("wrappers nested too deeply");
  let words = command.words;
  while (words.length > 0) {
    const first = words[0].text;
    if (NOT_A_COMMAND.has(first)) return [];
    if (first === "function") words = words.slice(2);
    else if (RESERVED.has(first) || ASSIGNMENT.test(first))
      words = words.slice(1);
    else break;
  }
  if (words.length === 0) return [];
  const name = basename(words[0].text);
  const rest = words.slice(1);
  const again = (inner) =>
    expand({ ...command, words: inner }, level + 1, nested);
  const wrapped = (
    inner,
    head = words.slice(0, words.length - inner.length),
  ) => [{ ...command, words: head, wrapper: true }, ...again(inner)];
  const script = (text) => {
    for (const inner of parseShell(text).commands)
      nested.push({ command: inner, level: level + 1 });
    return [];
  };
  const joined = (inner) => script(inner.map((word) => word.text).join(" "));

  if (name === "env") {
    const { head, inner } = unwrapEnv(rest);
    // env without a command prints the environment.
    if (inner.length === 0) return [{ ...command, words }];
    return wrapped(inner, [words[0], ...head]);
  }
  if (Object.hasOwn(RUNNERS, name)) {
    const inner = unwrapRunner(rest, RUNNERS[name]);
    return inner === null ? [{ ...command, words }] : wrapped(inner);
  }
  if (name === "command") {
    const inner = skipOptions(rest, []);
    const lookup = rest
      .slice(0, rest.length - inner.length)
      .some((word) => /^-[a-zA-Z]*[vV]/.test(word.text));
    return lookup ? [] : again(inner);
  }
  if (name === "timeout" || name === "gtimeout")
    return again(skipOptions(rest, ["-s", "-k"]).slice(1));
  if (Object.hasOwn(WRAPPERS, name))
    return wrapped(skipOptions(rest, WRAPPERS[name]));
  if (SHELLS.has(name)) {
    const run = shellScript(rest);
    if (run.script) script(run.script.text);
    // bash <<EOF and bash -s <<< '…' run their stdin.
    else if (run.file === false && command.stdin?.text !== undefined)
      script(command.stdin.text);
    // The shell's own words name its script file or the script's arguments.
    return [{ ...command, words, wrapper: true }];
  }
  if (name === "eval") return joined(rest);
  if (name === "watch") return joined(skipOptions(rest, ["-n", "--interval"]));
  if (name === "find") {
    const actions = [];
    for (let i = 0; i < rest.length; i++) {
      if (!FIND_ACTIONS.includes(rest[i].text)) continue;
      let end = i + 1;
      while (end < rest.length && ![";", "+"].includes(rest[end].text)) end++;
      actions.push(...again(rest.slice(i + 1, end)));
      i = end;
    }
    return [{ ...command, words }, ...actions];
  }
  return [{ ...command, words }];
}

// --- Paths ------------------------------------------------------------------

// Env files that hold secrets: .env, .env.local, .env.<anything>, except the
// tracked *.example templates. macOS file systems ignore case, so this does.
const isEnvFile = (name) => {
  const lower = name.toLowerCase();
  return /^\.env(?:\..+)?$/.test(lower) && !lower.endsWith(".example");
};
// Names a glob is tried against when no such file exists yet.
const ENV_SAMPLES = [".env", ".env.local", ".env.production", ".env.test"];
const PROC_ENVIRON = /^\/proc\/[^/]+\/environ$/;

const hasGlob = (text) => /[*?[]/.test(text);

// Matches one path segment against a shell glob, ignoring case. A leading dot
// must be matched literally, as in bash and zsh, unless dots is set (find
// -name, grep --include). A glob that is not valid matches nothing.
function globMatches(glob, name, { dots = false } = {}) {
  if (!hasGlob(glob)) return glob.toLowerCase() === name.toLowerCase();
  if (!dots && name.startsWith(".") && !glob.startsWith(".")) return false;
  let source = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    const close = c === "[" ? glob.indexOf("]", i + 2) : -1;
    if (c === "*") source += "[^/]*";
    else if (c === "?") source += "[^/]";
    else if (close !== -1) {
      const set = glob.slice(i + 1, close).replace(/^!/, "^");
      source += `[${set.replace(/\\/g, "\\\\")}]`;
      i = close;
    } else source += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  }
  try {
    return new RegExp(`^${source}$`, "i").test(name);
  } catch {
    return false;
  }
}

// Expands {a,b} and {,x} the way bash does, up to a bounded number of words.
function braceExpand(text, limit = 64) {
  let depth = 0;
  let open = -1;
  const commas = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "{" && depth++ === 0) open = i;
    else if (text[i] === "," && depth === 1) commas.push(i);
    else if (text[i] === "}" && depth > 0 && --depth === 0) {
      if (commas.length === 0) {
        open = -1;
        continue;
      }
      const cuts = [open, ...commas, i];
      const head = text.slice(0, open);
      const tail = text.slice(i + 1);
      const words = [];
      for (let k = 0; k + 1 < cuts.length && words.length < limit; k++)
        for (const word of braceExpand(
          head + text.slice(cuts[k] + 1, cuts[k + 1]) + tail,
          limit,
        ))
          if (words.length < limit) words.push(word);
      return words;
    }
  }
  return [text];
}

// The parts of a word that may name a file: each brace expansion, the value
// of --flag=value, the halves of host:path or rev:path, and @file. A part
// with whitespace is prose (a commit message, a PR body), not a path.
function candidates(text) {
  const found = new Set();
  for (const word of braceExpand(text)) {
    found.add(word);
    for (const separator of ["=", ":", ","])
      for (const part of word.split(separator)) found.add(part);
  }
  for (const part of [...found])
    if (part.startsWith("@")) found.add(part.slice(1));
  return [...found].filter((part) => part !== "" && !/\s/.test(part));
}

// Replaces the variables the guard knows: $X, ${X}, and ${X:-word} and its
// relatives, which fall back to word when X is unknown.
function substitute(text, vars) {
  return text.replace(
    /\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-=?+])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)/g,
    (match, braced, op, word, plain) => {
      const value = vars.get(braced ?? plain);
      if (op === undefined || op.endsWith("?")) return value ?? match;
      if (op.endsWith("+")) return value === undefined ? match : word;
      return value ?? substitute(word, vars);
    },
  );
}

function expandHome(text, home) {
  if (text === "~") return home;
  return text.startsWith("~/") ? path.join(home, text.slice(2)) : text;
}

// Whether a path, which may hold globs, can name dir or something inside it.
function within(full, dir) {
  if (!hasGlob(full)) {
    const [a, b] = [full.toLowerCase(), dir.toLowerCase()];
    return a === b || a.startsWith(`${b}/`);
  }
  const pattern = full.split("/");
  const target = dir.split("/");
  for (let i = 0; i < target.length; i++) {
    if (pattern[i] === "**") return true;
    if (i >= pattern.length || !globMatches(pattern[i], target[i]))
      return false;
  }
  return true;
}

function envFileMatch(value, context) {
  const name = basename(value);
  if (!hasGlob(name)) return isEnvFile(name);
  if (ENV_SAMPLES.some((sample) => globMatches(name, sample))) return true;
  const dir = path.dirname(value);
  if (hasGlob(dir)) return false;
  try {
    return readdirSync(path.resolve(context.cwd, dir)).some(
      (entry) => isEnvFile(entry) && globMatches(name, entry),
    );
  } catch {
    return false;
  }
}

// What a resolved path names: an env file, a process environment, or a
// place inside a secrets directory; null for none of these.
function secretTarget(full, context) {
  if (isEnvFile(basename(full))) return "the env file";
  if (PROC_ENVIRON.test(full)) return "the environment of a process";
  const dir = context.secretDirs.find((secret) => within(full, secret));
  return dir === undefined ? null : `inside ${context.shown(dir)}`;
}

function realpath(full) {
  try {
    return realpathSync(full);
  } catch {
    return full;
  }
}

// Returns what makes a word name a secret, or null. Symbolic links are
// followed, so a link to .env counts as .env.
function secretPath(text, context) {
  for (const candidate of candidates(text)) {
    const value = expandHome(substitute(candidate, context.vars), context.home);
    if (envFileMatch(value, context)) return `the env file ${candidate}`;
    const full = path.resolve(context.cwd, value);
    if (PROC_ENVIRON.test(full)) return "the environment of a process";
    const dir = context.secretDirs.find((secret) => within(full, secret));
    if (dir !== undefined) return `${candidate}, inside ${context.shown(dir)}`;
    const target = hasGlob(full) ? null : secretTarget(realpath(full), context);
    if (target !== null) return `${candidate}, a link to ${target}`;
  }
  return null;
}

// Variables whose value is likely a secret: upper-case names with one of
// these parts, so $MAX_TOKENS and a loop's $key stay ordinary.
const SECRET_VARIABLE =
  /(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIALS?|DSN)(?:_|$)|DATABASE_URL|DB__URL|MIGRATION_URL/;

// A variable counts as secret by its name or because the guard saw it
// assigned from a secret one (X=$GH_TOKEN).
function secretVariable(names, context) {
  return (
    names.find(
      (name) => SECRET_VARIABLE.test(name) || context.secretVars.has(name),
    ) ?? null
  );
}

// The variables an unquoted heredoc body expands; \$X stays literal.
const variablesIn = (text) =>
  [...text.matchAll(/(?<!\\)\$\{?[#!]?([A-Za-z_][A-Za-z0-9_]*)/g)].map(
    (match) => match[1],
  );

// --- Checks -----------------------------------------------------------------

// Commands that may name an env file: they look at names, not contents.
const METADATA = new Set([
  "ls",
  "test",
  "[",
  "[[",
  "stat",
  "du",
  "chmod",
  "realpath",
  "dirname",
  "basename",
  "cd",
  "pushd",
]);
const METADATA_GIT = new Set(["check-ignore", "ls-files", "status"]);
const FIND_WRITES = ["-delete", "-fls", "-fprint", "-fprint0", "-fprintf"];
// Commands whose operands are only compared, never printed.
const TESTS = new Set(["test", "[", "[["]);
const DECLARES = new Set(["export", "declare", "typeset", "local", "readonly"]);
// Commands that turn a pipe's output into arguments or run it as a script.
const PIPE_READERS = new Set(["xargs", "while", "parallel"]);

// A name a pipe feeds into a reader is a path, even from ls or find.
function isMetadata(program, args, piped) {
  if (piped) return false;
  if (METADATA.has(program)) return true;
  if (program === "git") return METADATA_GIT.has(gitCommand(args).sub);
  if (program === "find")
    return !args.some(
      (arg) => FIND_ACTIONS.includes(arg) || FIND_WRITES.includes(arg),
    );
  return false;
}

// The first word that runs, past assignments.
function programOf(command) {
  const word = command?.words.find(
    (candidate) => !ASSIGNMENT.test(candidate.text),
  );
  return word === undefined ? null : basename(word.text);
}

// Checks what belongs to the whole command line: secret variables it
// expands, files it redirects to or from, and the words of a for loop. A
// secret may flow into an assignment or a test; the variable it lands in
// counts as secret from then on (track).
function checkCommand(command, context) {
  const words = command.words;
  let start = 0;
  while (start < words.length && ASSIGNMENT.test(words[start].text)) start++;
  const program = programOf(command);
  const shown = TESTS.has(program)
    ? []
    : words
        .slice(start)
        .filter(
          (word, i) =>
            !(i > 0 && DECLARES.has(program) && ASSIGNMENT.test(word.text)),
        );
  const expanded = [...shown, ...command.redirects].flatMap(
    (word) => word.vars,
  );
  if (command.stdin?.expands) expanded.push(...variablesIn(command.stdin.text));
  const variable = secretVariable(expanded, context);
  if (variable !== null)
    return { rule: "secrets", detail: `the variable $${variable}` };
  for (const word of [...command.redirects, ...loopWords(words)]) {
    const hit = secretPath(word.text, context);
    if (hit !== null) return { rule: "secrets", detail: hit };
  }
  return null;
}

function loopWords(words) {
  if (!["for", "select"].includes(words[0]?.text)) return [];
  const at = words.findIndex((word) => word.text === "in");
  return at === -1 ? [] : words.slice(at + 1);
}

function checkInvocation(invocation, context) {
  const words = invocation.words;
  const program = basename(words[0].text);
  const args = words.slice(1).map((word) => word.text);
  const piped = invocation.piped === true;
  // A wrapper's own options (xargs -a file, env NAME=value) can name a path.
  if (invocation.wrapper) return checkPaths(program, words, context, piped);
  return (
    checkPaths(program, words, context, piped) ??
    checkInlineCode(words, invocation.stdin) ??
    checkProgram(program, args, invocation, context) ??
    checkRecursive(program, args, context, piped)
  );
}

function checkPaths(program, words, context, piped) {
  const args = words.slice(1).map((word) => word.text);
  if (isMetadata(program, args, piped)) return null;
  const skip = new Set(
    [...searchPatterns(program, args), ...excludeValues(program, args)].map(
      (at) => at + 1,
    ),
  );
  for (let i = 0; i < words.length; i++) {
    if (skip.has(i)) continue;
    const hit = secretPath(words[i].text, context);
    if (hit !== null) return { rule: "secrets", detail: hit };
  }
  return null;
}

// Indexes in args of rsync's and tar's --exclude patterns.
function excludeValues(program, args) {
  if (program !== "rsync" && program !== "tar") return [];
  const found = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--exclude=")) found.push(i);
    else if (args[i] === "--exclude") found.push(++i);
  }
  return found;
}

// Code passed inline to an interpreter (python -c, node -e, awk '…', or a
// heredoc on its stdin).
const INTERPRETER =
  /^(?:python(?:\d+(?:\.\d+)?)?|pypy3?|node|nodejs|tsx|ts-node|deno|bun|perl|ruby|irb|php|osascript|[gmn]?awk|lua|Rscript)$/;
const EMBEDDED_ENV_FILE = /(?<![\w.-])\.env(?:\.[\w-]+)*(?![\w.-])/gi;
const ENVIRONMENT_ACCESS =
  /\benvironb?\b|\bgetenv\b|\bprocess\s*(?:\.\s*env\b|\[)|\bDeno\.env\b|\bBun\.env\b|\$_ENV\b|%ENV\b|\$ENV\{|\bENV\b|\bENVIRON\b/;

function checkInlineCode(words, stdin) {
  const at = words.findIndex((word) => INTERPRETER.test(basename(word.text)));
  if (at === -1) return null;
  const code = words.slice(at + 1).map((word) => word.text);
  if (stdin?.text !== undefined) code.push(stdin.text);
  for (const text of code) {
    const file = [...text.matchAll(EMBEDDED_ENV_FILE)]
      .map((match) => match[0])
      .find(isEnvFile);
    if (file !== undefined)
      return { rule: "secrets", detail: `inline code that names ${file}` };
    if (/\.config\/skillforge/i.test(text))
      return {
        rule: "secrets",
        detail: "inline code that names ~/.config/skillforge",
      };
    if (ENVIRONMENT_ACCESS.test(text))
      return {
        rule: "secrets",
        detail: "inline code that reads the environment",
      };
  }
  return null;
}

const REMOTE_SHELLS = new Set([
  "ssh",
  "scp",
  "sftp",
  "sshfs",
  "ssh-add",
  "ssh-copy-id",
  "slogin",
  "mosh",
  "autossh",
  "sshpass",
]);

function checkProgram(program, args, invocation, context) {
  const dump = (detail) => ({
    rule: "secrets",
    detail: `an environment dump (${detail})`,
  });
  const options = (list) => list.every((arg) => /^[-+]/.test(arg));
  switch (program) {
    case "printenv":
      return dump("printenv");
    case "env":
      return dump("env without a command");
    case "set":
      return args.length === 0 ? dump("set") : null;
    case "export":
    case "readonly":
      return options(args) ? dump(`${program} ${args.join(" ")}`.trim()) : null;
    case "declare":
    case "typeset":
      return options(args) || args.some((arg) => /^-[A-Za-z]*[pm]/.test(arg))
        ? dump(`${program} ${args.join(" ")}`.trim())
        : null;
    case "ps":
      return psShowsEnvironment(args) ? dump("ps with the environment") : null;
    case "jq":
      return jqReadsEnvironment(args) ? dump("jq env") : null;
    case "launchctl":
      return ["getenv", "print", "export"].includes(args[0])
        ? dump(`launchctl ${args[0]}`)
        : null;
    case "docker":
    case "docker-compose": {
      const detail = dockerDump(program, args);
      return detail === null ? null : dump(detail);
    }
    case "rsync":
      return rsyncIsRemote(args)
        ? { rule: "remote-shell", detail: "rsync to or from a host" }
        : null;
    case "git":
      return checkGit(args);
    case "gh":
    case "gh.exe":
      return checkGh(args, invocation.stdin, context);
    case "gh-stack":
      return args.find((arg) => !arg.startsWith("-")) === "merge"
        ? { rule: "stack-merge" }
        : null;
    default:
      return REMOTE_SHELLS.has(program)
        ? { rule: "remote-shell", detail: program }
        : null;
  }
}

// ps prints the environment with -E, or with e in BSD-style options (ps eww);
// -e only selects every process.
function psShowsEnvironment(args) {
  return args.some(
    (arg) =>
      /^-[A-Za-z]*E/.test(arg) ||
      (/^[A-Za-z]+$/.test(arg) && arg.includes("e")),
  );
}

function jqFilter(args) {
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "-f" || arg === "--from-file") return null;
    if (["--arg", "--argjson", "--slurpfile", "--rawfile"].includes(arg))
      i += 2;
    else if (["--indent", "-L"].includes(arg)) i++;
    else if (!arg.startsWith("-")) return i;
  }
  return null;
}

// jq's env and $ENV read the environment; "env" in a string or an object key
// does not.
function jqReadsEnvironment(args) {
  const at = jqFilter(args);
  if (at === null) return false;
  const code = args[at].replace(/"(?:[^"\\]|\\.)*"/g, '""');
  return /(?<![\w.$])env\b(?!\s*:)|\$ENV\b/.test(code);
}

const DOCKER_VALUES = [
  "--context",
  "-c",
  "-H",
  "--host",
  "--config",
  "-l",
  "--log-level",
];
const COMPOSE_VALUES = [
  "-f",
  "--file",
  "-p",
  "--project-name",
  "--profile",
  "--project-directory",
  "--env-file",
  "--ansi",
  "--progress",
  "--parallel",
];
// docker compose config prints values only with none of these.
const COMPOSE_LISTS = [
  "-q",
  "--quiet",
  "--services",
  "--volumes",
  "--profiles",
  "--images",
  "--networks",
  "--hash",
];

const firstPositional = (args, withValue) => {
  for (let i = 0; i < args.length; i++) {
    if (withValue.includes(args[i])) i++;
    else if (!args[i].startsWith("-")) return i;
  }
  return -1;
};

// Returns why a docker command prints the environment, or null: docker
// inspect, docker compose config or convert, and env or printenv run inside
// a container.
function dockerDump(program, args) {
  let rest = args;
  if (program === "docker") {
    const at = firstPositional(args, DOCKER_VALUES);
    if (args[at] === "inspect") return "docker inspect";
    if (args[at] !== "compose") return null;
    rest = args.slice(at + 1);
  }
  const at = firstPositional(rest, COMPOSE_VALUES);
  const sub = rest[at];
  const after = rest.slice(at + 1);
  if (["config", "convert"].includes(sub))
    return after.some((arg) => COMPOSE_LISTS.includes(arg))
      ? null
      : `docker compose ${sub} fills in the env file`;
  if (
    ["exec", "run"].includes(sub) &&
    after.some((arg) => ["env", "printenv"].includes(basename(arg)))
  )
    return `env inside a container`;
  return null;
}

const RSYNC_VALUES = new Set([
  "--exclude",
  "--include",
  "--filter",
  "-f",
  "--rsync-path",
  "-T",
  "--temp-dir",
  "--backup-dir",
  "--suffix",
  "--chmod",
  "--chown",
  "--usermap",
  "--groupmap",
  "--log-file",
  "--log-file-format",
  "--out-format",
  "--password-file",
  "--files-from",
  "--exclude-from",
  "--include-from",
  "-B",
  "--block-size",
  "--bwlimit",
  "--timeout",
  "--port",
]);

function rsyncPositionals(args) {
  const found = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--") return [...found, ...args.slice(i + 1)];
    if (RSYNC_VALUES.has(args[i])) i++;
    else if (!args[i].startsWith("-")) found.push(args[i]);
  }
  return found;
}

function rsyncIsRemote(args) {
  if (
    args.some(
      (arg) => /^(?:-e|--rsh)(?:=|$)/.test(arg) || /^-[A-Za-z]*e$/.test(arg),
    )
  )
    return true;
  return rsyncPositionals(args).some(
    (arg) => /^rsync:\/\//i.test(arg) || /^[^/]*:/.test(arg),
  );
}

// --- git --------------------------------------------------------------------

const GIT_VALUES = [
  "-C",
  "-c",
  "--git-dir",
  "--work-tree",
  "--namespace",
  "--super-prefix",
  "--config-env",
  "--attr-source",
];

// Returns the git subcommand and its arguments, past git's own options.
function gitCommand(args) {
  for (let i = 0; i < args.length; i++) {
    if (GIT_VALUES.includes(args[i])) i++;
    else if (!args[i].startsWith("-"))
      return { sub: args[i], rest: args.slice(i + 1) };
  }
  return { sub: null, rest: [] };
}

function checkGit(args) {
  const { sub, rest } = gitCommand(args);
  if (sub === "tag") return { rule: "repo-admin", detail: "git tag" };
  if (sub === "update-ref" && rest.some((arg) => arg.includes("refs/tags/")))
    return { rule: "repo-admin", detail: "git update-ref of a tag" };
  if (sub !== "push") return null;
  const positionals = rest.filter((arg) => !arg.startsWith("-"));
  const tags =
    rest.some((arg) => ["--tags", "--follow-tags", "--mirror"].includes(arg)) ||
    positionals.some((arg) => arg.includes("refs/tags/")) ||
    positionals.slice(1).includes("tag");
  return tags ? { rule: "repo-admin", detail: "git push of tags" } : null;
}

// --- Search -----------------------------------------------------------------

// Options that take a value, short letters and long names, per search tool.
const SEARCH_OPTIONS = {
  grep: {
    short: "efmABCdD",
    long: [
      "regexp",
      "file",
      "max-count",
      "after-context",
      "before-context",
      "context",
      "directories",
      "devices",
      "label",
      "include",
      "exclude",
      "exclude-dir",
      "exclude-from",
      "binary-files",
      "group-separator",
    ],
  },
  rg: {
    short: "efgtTmABCjMrEd",
    long: [
      "regexp",
      "file",
      "glob",
      "iglob",
      "type",
      "type-not",
      "type-add",
      "type-clear",
      "max-count",
      "after-context",
      "before-context",
      "context",
      "threads",
      "max-columns",
      "replace",
      "encoding",
      "max-depth",
      "max-filesize",
      "pre",
      "pre-glob",
      "ignore-file",
      "sort",
      "sortr",
      "path-separator",
      "colors",
      "engine",
      "context-separator",
    ],
  },
  "git grep": {
    short: "efmABCO",
    long: [
      "max-depth",
      "max-count",
      "after-context",
      "before-context",
      "context",
      "threads",
      "open-files-in-pager",
    ],
  },
};
const GREPS = new Set(["grep", "egrep", "fgrep", "rgrep"]);

// Parses search-tool arguments into options ({ flag, value, at }) and
// positionals ({ text, at }), with at the index in args.
function parseSearch(spec, args) {
  const options = [];
  const positionals = [];
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--") {
      for (let j = i + 1; j < args.length; j++)
        positionals.push({ text: args[j], at: j });
      break;
    }
    if (arg.startsWith("--")) {
      const equals = arg.indexOf("=");
      const flag = arg.slice(2, equals === -1 ? undefined : equals);
      if (equals !== -1)
        options.push({ flag, value: arg.slice(equals + 1), at: i });
      else if (spec.long.includes(flag))
        options.push({ flag, value: args[i + 1] ?? "", at: ++i });
      else options.push({ flag, value: null, at: i });
    } else if (arg.startsWith("-") && arg.length > 1) {
      for (let k = 1; k < arg.length; k++) {
        if (!spec.short.includes(arg[k])) {
          options.push({ flag: arg[k], value: null, at: i });
          continue;
        }
        const attached = arg.slice(k + 1);
        if (attached !== "")
          options.push({ flag: arg[k], value: attached, at: i });
        else options.push({ flag: arg[k], value: args[i + 1] ?? "", at: ++i });
        break;
      }
    } else positionals.push({ text: arg, at: i });
  }
  return { options, positionals };
}

// Returns { spec, args } for grep, rg and git grep, or null.
function searchTool(program, args) {
  if (GREPS.has(program)) return { spec: SEARCH_OPTIONS.grep, args };
  if (program === "rg") return { spec: SEARCH_OPTIONS.rg, args };
  if (program === "git") {
    const { sub, rest } = gitCommand(args);
    if (sub === "grep")
      return {
        spec: SEARCH_OPTIONS["git grep"],
        args: rest,
        offset: args.length - rest.length,
      };
  }
  return null;
}

// Filters that only narrow a search: grep's --include and --exclude (the walk
// in checkRecursive applies them) and rg's exclusion globs. An rg inclusion
// glob overrides rg's ignore rules, so it stays a path.
const isSearchFilter = (option) =>
  ["include", "exclude", "exclude-dir"].includes(option.flag) ||
  (["g", "glob", "iglob"].includes(option.flag) &&
    option.value.startsWith("!"));

// Indexes in args of the search patterns and filters, which are text, not
// paths.
function searchPatterns(program, args) {
  if (program === "jq") {
    const at = jqFilter(args);
    return at === null ? [] : [at];
  }
  const tool = searchTool(program, args);
  if (tool === null) return [];
  const offset = tool.offset ?? 0;
  const { options, positionals } = parseSearch(tool.spec, tool.args);
  const explicit = options.filter((option) =>
    ["e", "regexp"].includes(option.flag),
  );
  const fromFile = options.some((option) =>
    ["f", "file"].includes(option.flag),
  );
  const patterns = [...explicit, ...options.filter(isSearchFilter)].map(
    (option) => option.at,
  );
  if (explicit.length === 0 && !fromFile && positionals.length > 0)
    patterns.push(positionals[0].at);
  return patterns.map((at) => at + offset);
}

// Returns the directories a command reads recursively, with the filters that
// narrow which files it reads, or null when it reads no tree. rg and git grep
// skip ignored files unless told otherwise, so they count only then. find
// counts when it runs an action or feeds a pipe into a reader.
function recursiveRead(program, args, piped) {
  const tool = searchTool(program, args);
  if (tool !== null) return searchRead(program, tool);
  const none = { include: [], exclude: [], excludeDir: [] };
  const positionals = args.filter((arg) => !arg.startsWith("-"));
  const flag = (letters, ...long) =>
    args.some(
      (arg) =>
        new RegExp(`^-[A-Za-z]*[${letters}]`).test(arg) || long.includes(arg),
    );
  switch (program) {
    case "find":
      return piped || args.some((arg) => FIND_ACTIONS.includes(arg))
        ? findRead(args)
        : null;
    case "cp":
      return flag("rRa", "--recursive", "--archive")
        ? { roots: positionals.slice(0, -1), filters: none }
        : null;
    case "ditto":
      return { roots: positionals.slice(0, -1), filters: none };
    case "rsync": {
      const exclude = excludeValues(program, args).map((at) =>
        args[at].replace(/^--exclude=/, ""),
      );
      return {
        roots: rsyncPositionals(args).slice(0, -1),
        filters: { include: [], exclude, excludeDir: exclude },
      };
    }
    case "tar":
      return tarRead(args);
    case "zip":
      return flag("r", "--recurse-paths")
        ? { roots: positionals.slice(1), filters: none }
        : null;
    case "diff":
      return flag("rR", "--recursive")
        ? { roots: positionals, filters: none }
        : null;
    case "git": {
      const { sub, rest } = gitCommand(args);
      const force = rest.some(
        (arg) => /^-[A-Za-z]*f$/.test(arg) || arg === "--force",
      );
      const paths = rest.filter((arg) => !arg.startsWith("-"));
      return sub === "add" && force && paths.length > 0
        ? { roots: paths, filters: none }
        : null;
    }
    default:
      return null;
  }
}

function searchRead(program, tool) {
  const { options, positionals } = parseSearch(tool.spec, tool.args);
  const has = (...flags) =>
    options.some((option) => flags.includes(option.flag));
  const values = (...flags) =>
    options
      .filter((option) => flags.includes(option.flag))
      .map((option) => option.value);
  const explicit = has("e", "regexp", "f", "file");
  const roots = positionals
    .slice(explicit ? 0 : 1)
    .map((positional) => positional.text);
  const filters = { include: [], exclude: [], excludeDir: [] };
  if (GREPS.has(program)) {
    const recursive =
      program === "rgrep" ||
      has("r", "R", "recursive", "dereference-recursive") ||
      values("d", "directories").includes("recurse");
    if (!recursive) return null;
    filters.include = values("include");
    filters.exclude = values("exclude");
    filters.excludeDir = values("exclude-dir");
  } else if (program === "rg") {
    const unrestricted =
      options.filter((option) => option.flag === "u").length +
      options.filter((option) => option.flag === "unrestricted").length;
    const hidden = has("hidden", ".") || unrestricted >= 2;
    const ignored = has("no-ignore", "no-ignore-vcs") || unrestricted >= 1;
    if (!hidden || !ignored) return null;
    for (const glob of values("g", "glob", "iglob")) {
      if (glob.startsWith("!")) filters.exclude.push(glob.slice(1));
      else filters.include.push(glob);
    }
  } else if (!has("no-index", "no-exclude-standard")) return null;
  return { roots: roots.length > 0 ? roots : ["."], filters };
}

// find's starting points come before its first expression; -name and -iname
// narrow the files it hands on, unless an expression is negated.
function findRead(args) {
  const at = args.findIndex(
    (arg) => arg.startsWith("-") || arg === "(" || arg === "!",
  );
  const roots = at === -1 ? args : args.slice(0, at);
  const include = [];
  if (!args.includes("!") && !args.includes("-not"))
    for (let i = 0; i < args.length; i++)
      if (["-name", "-iname"].includes(args[i])) include.push(args[++i] ?? "");
  return {
    roots: roots.length > 0 ? roots : ["."],
    filters: { include, exclude: [], excludeDir: [] },
  };
}

// tar reads a tree when it creates an archive (tar czf out.tgz dir).
function tarRead(args) {
  let create = false;
  let i = 0;
  const roots = [];
  const exclude = excludeValues("tar", args).map((at) =>
    args[at].replace(/^--exclude=/, ""),
  );
  const skip = new Set(excludeValues("tar", args));
  if (args[0] !== undefined && !args[0].startsWith("-")) {
    create = args[0].includes("c");
    i = 1 + [...args[0]].filter((letter) => "fCbT".includes(letter)).length;
  }
  for (; i < args.length; i++) {
    const arg = args[i];
    if (skip.has(i) || arg.startsWith("--exclude")) continue;
    if (arg === "--create" || /^-[A-Za-z]*c/.test(arg)) create = true;
    if (
      /^-[A-Za-z]*[fCbT]$/.test(arg) ||
      ["--file", "--directory", "--files-from"].includes(arg)
    )
      i++;
    else if (!arg.startsWith("-")) roots.push(arg);
  }
  return create
    ? { roots, filters: { include: [], exclude, excludeDir: exclude } }
    : null;
}

// Directories a recursive read walks into that never hold the secrets
// guarded here; skipping them keeps the walk fast.
const SKIP_DIRS = new Set([
  ".git",
  "node_modules",
  ".venv",
  "venv",
  "__pycache__",
  ".next",
  ".turbo",
  ".pnpm-store",
  ".mypy_cache",
  ".ruff_cache",
  ".pytest_cache",
]);
// Entries one command may walk in total, so the hook stays far below its
// timeout (a hook that times out does not block).
const WALK_LIMIT = 50_000;

function checkRecursive(program, args, context, piped) {
  const read = recursiveRead(program, args, piped);
  if (read === null) return null;
  const budget = { left: WALK_LIMIT };
  for (const root of new Set(read.roots)) {
    const hit = secretInTree(root, read.filters, context, budget);
    if (hit !== null) return { rule: "secrets", detail: hit };
  }
  return null;
}

function secretInTree(root, filters, context, budget) {
  let value = expandHome(substitute(root, context.vars), context.home);
  // A glob is walked from its last directory without one.
  if (hasGlob(value)) {
    const parts = value.split("/");
    value = parts.slice(0, parts.findIndex(hasGlob)).join("/") || ".";
  }
  const start = realpath(path.resolve(context.cwd, value));
  const dir = context.secretDirs.find((secret) => within(secret, start));
  if (dir !== undefined) return `${root}, which contains ${context.shown(dir)}`;
  const matches = (globs, name) =>
    globs.some((glob) => globMatches(glob, name, { dots: true }));
  const included = (name) =>
    !matches(filters.exclude, name) &&
    (filters.include.length === 0 || matches(filters.include, name));
  try {
    if (!statSync(start).isDirectory()) return null;
  } catch {
    return null;
  }
  const queue = [start];
  while (queue.length > 0) {
    const current = queue.shift();
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (--budget.left < 0) return `${root}, too large to check for env files`;
      const full = path.join(current, entry.name);
      const shown = path.relative(context.cwd, full);
      if (entry.isDirectory()) {
        if (
          !SKIP_DIRS.has(entry.name) &&
          !matches(filters.excludeDir, entry.name)
        )
          queue.push(full);
      } else if (isEnvFile(entry.name) && included(entry.name))
        return `the env file ${shown}, under ${root}`;
      else if (entry.isSymbolicLink() && included(entry.name)) {
        // A link is not followed into, only resolved.
        const target = secretTarget(realpath(full), context);
        if (target !== null) return `${shown}, a link to ${target}`;
      }
    }
  }
  return null;
}

// --- gh ---------------------------------------------------------------------

// Returns the indexes of the first two positional arguments of gh, which name
// the command (pr merge, repo delete, …).
function commandPath(args) {
  const found = [];
  for (let i = 0; i < args.length && found.length < 2; i++) {
    if (args[i] === "--") break;
    if (args[i] === "-R" || args[i] === "--repo") i++;
    else if (!args[i].startsWith("-")) found.push(i);
  }
  return found;
}

const GH_ADMIN = new Set(["secret", "variable", "ruleset", "rs", "release"]);

function checkGh(args, stdin, context) {
  const [groupAt, commandAt] = commandPath(args);
  const group = args[groupAt];
  const sub = args[commandAt];
  if (group === "pr" && sub === "merge") return { rule: "pr-merge" };
  if (group === "stack" && sub === "merge") return { rule: "stack-merge" };
  // gh extension exec stack merge runs the same extension.
  const words = args.slice(commandAt + 1).filter((arg) => !arg.startsWith("-"));
  if (
    ["extension", "ext", "extensions"].includes(group) &&
    sub === "exec" &&
    words[0] === "stack" &&
    words[1] === "merge"
  )
    return { rule: "stack-merge" };
  if (GH_ADMIN.has(group)) return { rule: "repo-admin", detail: `gh ${group}` };
  if (
    group === "repo" &&
    ["delete", "archive", "rename", "transfer"].includes(sub)
  )
    return { rule: "repo-destructive" };
  if (
    group === "repo" &&
    sub === "edit" &&
    args.some(
      (arg) => arg === "--visibility" || arg.startsWith("--visibility="),
    )
  )
    return { rule: "repo-destructive" };
  if (group === "repo" && sub === "edit")
    return { rule: "repo-admin", detail: "gh repo edit" };
  if (
    group === "auth" &&
    (sub === "token" ||
      (sub === "status" &&
        args.some(
          (arg) => arg.startsWith("--show-token") || /^-[A-Za-z]*t/.test(arg),
        )))
  )
    return { rule: "secrets", detail: `gh auth ${sub} prints a token` };
  if (group === "alias" && ["set", "import"].includes(sub))
    return { rule: "gh-alias" };
  if (["codespace", "cs"].includes(group) && ["ssh", "cp"].includes(sub))
    return { rule: "remote-shell", detail: `gh ${group} ${sub}` };
  if (group === "issue" && ["delete", "transfer"].includes(sub))
    return { rule: "issue-destructive" };
  if (group === "project" && ["delete", "field-delete"].includes(sub))
    return { rule: "project-destructive" };
  if (group === "label" && sub === "delete") return { rule: "label-delete" };
  if (group === "api") {
    const rest = [...args.slice(0, groupAt), ...args.slice(groupAt + 1)];
    return checkApi(rest, stdin, context);
  }
  return null;
}

// Flags of gh api that take a value, short and long; the others are booleans.
const API_SHORT_VALUES = "XfFHqtp";
const API_LONG_VALUES = new Set([
  "method",
  "field",
  "raw-field",
  "header",
  "jq",
  "template",
  "cache",
  "hostname",
  "input",
  "preview",
]);

// Parses gh api arguments the way gh's flag parser does.
function parseApiArgs(args) {
  const request = { method: null, fields: [], input: null, endpoint: null };
  const take = (flag, value) => {
    if (flag === "X" || flag === "method") request.method = value.toUpperCase();
    else if (["f", "raw-field", "F", "field"].includes(flag)) {
      const equals = value.indexOf("=");
      request.fields.push({
        typed: flag === "F" || flag === "field",
        key: equals === -1 ? value : value.slice(0, equals),
        value: equals === -1 ? "" : value.slice(equals + 1),
      });
    } else if (flag === "input") request.input = value;
  };
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--") {
      request.endpoint ??= args[i + 1] ?? null;
      break;
    }
    if (arg.startsWith("--")) {
      const equals = arg.indexOf("=");
      const flag = arg.slice(2, equals === -1 ? undefined : equals);
      if (equals !== -1) take(flag, arg.slice(equals + 1));
      else if (API_LONG_VALUES.has(flag)) take(flag, args[++i] ?? "");
    } else if (arg.startsWith("-") && arg.length > 1) {
      for (let k = 1; k < arg.length; k++) {
        if (!API_SHORT_VALUES.includes(arg[k])) continue;
        const attached = arg.slice(k + 1).replace(/^=/, "");
        take(arg[k], attached !== "" ? attached : (args[++i] ?? ""));
        break;
      }
    } else request.endpoint ??= arg;
  }
  return request;
}

function normaliseEndpoint(endpoint) {
  return endpoint
    .replace(/^https?:\/\/[^/]+\/(?:api\/v3\/)?/, "")
    .replace(/^\/+/, "")
    .replace(/[?#].*$/, "")
    .replace(/\/+$/, "");
}

function checkApi(args, stdin, context) {
  const request = parseApiArgs(args);
  if (request.endpoint === null) return null;
  const body = request.fields.length > 0 || request.input !== null;
  const method = request.method ?? (body ? "POST" : "GET");
  const endpoint = normaliseEndpoint(request.endpoint);
  if (/(?:^|\/)graphql$/.test(endpoint))
    return checkGraphql(request, stdin, context);
  if (method === "GET") return null;
  if (
    REST_WRITES.some(
      ([allowed, route]) => allowed === method && route.test(endpoint),
    )
  )
    return null;
  return {
    rule: /^repos\/[^/]+\/[^/]+\/pulls\/[^/]+\/merge$/.test(endpoint)
      ? "pr-merge"
      : "api-write",
    detail: `${method} ${endpoint}`,
  };
}

function checkGraphql(request, stdin, context) {
  const document = graphqlDocument(request, stdin, context);
  const operations = document === null ? null : parseGraphql(document);
  if (operations === null) return { rule: "api-unreadable" };
  const blocked = operations
    .filter((operation) => operation.type !== "query")
    .flatMap((operation) => operation.fields)
    .filter(
      (field) => !GRAPHQL_MUTATIONS.includes(field) && field !== "__typename",
    );
  if (blocked.length === 0) return null;
  const merge = blocked.some(
    (field) => /merge/i.test(field) && /pullrequest/i.test(field),
  );
  return {
    rule: merge ? "pr-merge" : "api-write",
    detail: `mutation ${blocked.map((field) => (field === "..." ? "fragment spread" : field)).join(", ")}`,
  };
}

// Returns the GraphQL document gh api would send, or null when it is unknown.
function graphqlDocument(request, stdin, context) {
  if (request.input !== null) {
    const body = readSource(request.input, stdin, context);
    try {
      const query = JSON.parse(body ?? "").query;
      return typeof query === "string" ? query : null;
    } catch {
      return null;
    }
  }
  const field = request.fields.findLast(
    (candidate) => candidate.key === "query",
  );
  if (field === undefined) return null;
  if (field.typed && field.value.startsWith("@"))
    return readSource(field.value.slice(1), stdin, context);
  return field.value;
}

function readSource(file, stdin, context) {
  if (file !== "-") return readFile(file, context);
  if (stdin?.text !== undefined) return stdin.text;
  if (stdin?.file !== undefined) return readFile(stdin.file, context);
  return null;
}

function readFile(file, context) {
  try {
    const full = path.resolve(context.cwd, expandHome(file, context.home));
    const stats = statSync(full);
    if (!stats.isFile() || stats.size > 1_000_000) return null;
    return readFileSync(full, "utf8");
  } catch {
    return null;
  }
}

// --- GraphQL ----------------------------------------------------------------

// Splits a GraphQL document into tokens; string literals become '""'.
function graphqlTokens(document) {
  const tokens = [];
  const name = /[_A-Za-z][_0-9A-Za-z]*/y;
  const number = /-?\d[\d.eE+-]*/y;
  let i = 0;
  while (i < document.length) {
    const c = document[i];
    if (/[\s,]/.test(c)) i++;
    else if (c === "#") {
      while (i < document.length && !"\r\n".includes(document[i])) i++;
    } else if (document.startsWith('"""', i)) {
      let j = i + 3;
      while (j < document.length && !document.startsWith('"""', j))
        j += document.startsWith('\\"""', j) ? 4 : 1;
      if (j >= document.length) return null;
      tokens.push('""');
      i = j + 3;
    } else if (c === '"') {
      let j = i + 1;
      while (j < document.length && document[j] !== '"') {
        if (document[j] === "\n") return null;
        j += document[j] === "\\" ? 2 : 1;
      }
      if (j >= document.length) return null;
      tokens.push('""');
      i = j + 1;
    } else if (document.startsWith("...", i)) {
      tokens.push("...");
      i += 3;
    } else if ("!$&():=@[]{}|".includes(c)) {
      tokens.push(c);
      i++;
    } else {
      name.lastIndex = i;
      number.lastIndex = i;
      const match = name.exec(document) ?? number.exec(document);
      if (match === null) return null;
      tokens.push(match[0]);
      i += match[0].length;
    }
  }
  return tokens;
}

const isName = (token) => /^[_A-Za-z]/.test(token ?? "");

// Returns the operations of a GraphQL document as { type, fields }, where
// fields are the top-level field names of a mutation or subscription (aliases
// resolved, "..." for a fragment spread), or null when it does not parse.
function parseGraphql(document) {
  const tokens = graphqlTokens(document);
  if (tokens === null || tokens.length === 0) return null;
  const pairs = { "(": ")", "{": "}" };
  const skipGroup = (start) => {
    let level = 0;
    for (let t = start; t < tokens.length; t++) {
      if (tokens[t] === tokens[start]) level++;
      else if (tokens[t] === pairs[tokens[start]] && --level === 0)
        return t + 1;
    }
    return -1;
  };
  const topLevelFields = (start) => {
    const fields = [];
    let t = start + 1;
    while (t < tokens.length && tokens[t] !== "}") {
      if (tokens[t] === "...")
        return { fields: [...fields, "..."], end: tokens.length };
      if (!isName(tokens[t])) return null;
      let field = tokens[t++];
      if (tokens[t] === ":") {
        if (!isName(tokens[t + 1])) return null;
        field = tokens[t + 1];
        t += 2;
      }
      fields.push(field);
      if (tokens[t] === "(") t = skipGroup(t);
      while (t !== -1 && tokens[t] === "@") {
        t = isName(tokens[t + 1]) ? t + 2 : -1;
        if (t !== -1 && tokens[t] === "(") t = skipGroup(t);
      }
      if (t !== -1 && tokens[t] === "{") t = skipGroup(t);
      if (t === -1) return null;
    }
    return t < tokens.length ? { fields, end: t + 1 } : null;
  };

  const operations = [];
  let t = 0;
  while (t < tokens.length) {
    let type = "query";
    if (["query", "mutation", "subscription", "fragment"].includes(tokens[t]))
      type = tokens[t++];
    else if (tokens[t] !== "{") return null;
    while (t < tokens.length && tokens[t] !== "{") {
      if (tokens[t] === "(") t = skipGroup(t);
      else if (isName(tokens[t]) || ["$", "@", ":", "!"].includes(tokens[t]))
        t++;
      else return null;
      if (t === -1) return null;
    }
    if (t >= tokens.length) return null;
    if (type === "mutation" || type === "subscription") {
      const selection = topLevelFields(t);
      if (selection === null) return null;
      operations.push({ type, fields: selection.fields });
      t = selection.end;
    } else {
      t = skipGroup(t);
      if (t === -1) return null;
      operations.push({ type: "query", fields: [] });
    }
  }
  return operations;
}

// --- Entry ------------------------------------------------------------------

function createContext({ cwd, home, xdgConfigHome }) {
  const config = xdgConfigHome || path.join(home, ".config");
  // Each secrets directory as written and resolved, so links compare too.
  const secretDirs = [
    ...new Set(
      [
        path.join(home, ".config", "skillforge"),
        path.join(config, "skillforge"),
      ].flatMap((dir) => [dir, realpath(dir)]),
    ),
  ];
  const user = path.basename(home);
  return {
    cwd,
    home,
    secretDirs,
    vars: new Map([
      ["HOME", home],
      ["PWD", cwd],
      ["USER", user],
      ["LOGNAME", user],
      ["XDG_CONFIG_HOME", config],
    ]),
    // Variables assigned from a secret one.
    secretVars: new Set(),
    shown: (dir) =>
      dir.startsWith(`${home}/`) ? `~/${path.relative(home, dir)}` : dir,
  };
}

// Follows what later commands depend on: cd and plain variable assignments,
// including export NAME=value and a for loop over a single word.
function track(command, context) {
  const words = command.words;
  const set = (name, raw) => {
    const value = substitute(raw, context.vars);
    if (/[$`]/.test(value)) context.vars.delete(name);
    else context.vars.set(name, value);
    if (secretVariable(variablesIn(raw), context) !== null)
      context.secretVars.add(name);
    else context.secretVars.delete(name);
  };
  const first = words[0]?.text;
  if (first === "for" || first === "select") {
    const list = loopWords(words);
    if (list.length === 1) set(words[1].text, list[0].text);
    else context.vars.delete(words[1]?.text);
    return;
  }
  const declares = [
    "export",
    "declare",
    "typeset",
    "local",
    "readonly",
  ].includes(first);
  let i = declares ? 1 : 0;
  for (; i < words.length; i++) {
    const assignment = /^([A-Za-z_][A-Za-z0-9_]*)\+?=(.*)$/s.exec(
      words[i].text,
    );
    if (assignment !== null) set(assignment[1], assignment[2]);
    else if (!declares || !words[i].text.startsWith("-")) break;
  }
  let rest = words.slice(declares ? words.length : i);
  if (["builtin", "command"].includes(rest[0]?.text)) rest = rest.slice(1);
  if (["cd", "pushd"].includes(rest[0]?.text)) {
    const target = rest.slice(1).find((word) => !word.text.startsWith("-"));
    const value = expandHome(
      substitute(target?.text ?? "~", context.vars),
      context.home,
    );
    if (!/[$`]/.test(value)) {
      context.cwd = path.resolve(context.cwd, value);
      context.vars.set("PWD", context.cwd);
    }
  }
}

const shownWords = (words) => words.map((word) => word.text).join(" ");

function inspect(command, context, level) {
  const found = checkCommand(command, context);
  if (found !== null)
    return {
      ...found,
      command: [
        shownWords(command.words),
        ...command.redirects.map((word) => `${word.op} ${word.text}`),
      ]
        .join(" ")
        .trim(),
    };
  const nested = [];
  // What a pipe feeds: names that xargs or a read loop turns into paths, or
  // a script for a shell that reads its stdin (echo 'cmd' | sh).
  const consumer = programOf(command.next);
  const piped = PIPE_READERS.has(consumer);
  if (SHELLS.has(consumer) && ["echo", "printf"].includes(programOf(command))) {
    const shell = command.next.words.findIndex(
      (word) => basename(word.text) === consumer,
    );
    if (shellScript(command.next.words.slice(shell + 1)).file === false)
      for (const inner of parseShell(
        command.words
          .slice(1)
          .map((word) => word.text)
          .join(" "),
      ).commands)
        nested.push({ command: inner, level: level + 1 });
  }
  for (const found of expand(command, level, nested)) {
    const invocation = { ...found, piped };
    const result = checkInvocation(invocation, context);
    if (result !== null)
      return { ...result, command: shownWords(invocation.words) };
  }
  track(command, context);
  return inspectAll(nested, context);
}

function inspectAll(items, context) {
  for (const { command, level } of items) {
    const result = inspect(command, context, level);
    if (result !== null) return result;
  }
  return null;
}

// Returns null when the command may run, or { rule, command, detail } for the
// first invocation the guard blocks. Throws when it cannot check the command.
export function check(
  source,
  { cwd = process.cwd(), home = homedir(), xdgConfigHome } = {},
) {
  const context = createContext({ cwd, home, xdgConfigHome });
  const commands = parseShell(source).commands;
  return inspectAll(
    commands.map((command) => ({ command, level: 0 })),
    context,
  );
}

// The Monitor tool runs a shell command too.
const TOOLS = new Set(["Bash", "Monitor"]);

function main() {
  let input;
  try {
    input = JSON.parse(readFileSync(0, "utf8"));
  } catch {
    return;
  }
  const source = input?.tool_input?.command;
  if (!TOOLS.has(input?.tool_name) || typeof source !== "string") return;
  let result;
  try {
    result = check(source, {
      cwd: input.cwd ?? process.cwd(),
      xdgConfigHome: process.env.XDG_CONFIG_HOME,
    });
  } catch (error) {
    // A bug in the guard must not let a command through unchecked.
    result = { rule: "guard-error", command: source, detail: error.message };
  }
  if (result === null) return;
  process.stderr.write(`${message(result)}\n`);
  process.exitCode = 2;
}

const invoked =
  process.argv[1] !== undefined &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) main();

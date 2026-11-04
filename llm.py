"""Capa LLM: SDK de Anthropic si hay ANTHROPIC_API_KEY, si no el binario de Claude Code en modo headless."""
import json, os, re, subprocess, tempfile

_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):  # ANTHROPIC_API_KEY=sk-ant-... en .env (no se sube a ningun sitio)
    for line in open(_env):
        k, _, v = line.strip().partition("=")
        if k and v and not k.startswith("#"):
            os.environ.setdefault(k.strip(), v.strip().strip('"\''))

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/blueblood/.vscode/extensions/anthropic.claude-code-2.1.314-linux-x64/resources/native-binary/claude")
MODELS = {"fast": "claude-haiku-5-5", "smart": "claude-sonnet-5-5"}
CLI_MODELS = {"fast": "haiku", "smart": "sonnet"}


def backend():
    return "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"


def ask(prompt: str, system: str = "", tier: str = "fast", max_tokens: int = 4000) -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        r = anthropic.Anthropic().messages.create(
            model=MODELS[tier], max_tokens=max_tokens, system=system or anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}])
        return r.content[0].text
    full = (system + "\n\n" if system else "") + prompt
    r = subprocess.run([CLAUDE_BIN, "-p", full, "--model", CLI_MODELS[tier], "--output-format", "text"],
                       capture_output=True, text=True, timeout=180, cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])
    return r.stdout


def ask_json(prompt: str, system: str = "", tier: str = "fast", max_tokens: int = 4000) -> dict:
    out = ask(prompt + "\n\nResponde SOLO con un objeto JSON válido, sin texto alrededor ni ```.", system, tier, max_tokens)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError("LLM sin JSON: " + out[:300])
    return json.loads(m.group(0))

# Claude Research Skills

Skills for investment research with Claude Code.

## Install

Paste this sentence into Claude Code, either in the terminal or in the Code tab of the Claude desktop app:

> Install or update the skills from https://github.com/behavioralvalueinvestor/claude-research-skills. Download a fresh copy of the repo into a new temporary folder. For each folder inside its `skills/` folder, replace `~/.claude/skills/<same folder name>/` with an exact copy of it: delete that old folder if it exists, then copy the new one so its SKILL.md ends up at `~/.claude/skills/<same folder name>/SKILL.md`. Do not touch any other folder in `~/.claude/skills/`. Then list the full path of every file you installed.

Then quit and restart Claude Code or the desktop app, whichever you used. To update later, paste the same sentence again.

## What's inside

- **/10k-to-markdown**: downloads one 10-K from SEC EDGAR for a ticker and fiscal year, and saves it as a Markdown file on your computer, plus a small index of where each section starts and ends. Claude then reads only the sections it needs instead of the whole filing. Needs Python 3, with nothing else to install.

Example: `/10k-to-markdown MSFT 2025`

## License

Free to use under the MIT license. See [LICENSE](LICENSE).

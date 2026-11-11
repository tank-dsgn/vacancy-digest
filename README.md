# Vacancy digest

Every morning GitHub Actions runs `generate.py`. It collects UX/UI vacancies from rabota.by, hh.ru (remote), getmatch.ru, career.habr.com and designer.ru, reads the text of each one, checks it against the criteria, and sends a digest plus the full list to Telegram.

## Settings

Nothing personal lives in the code. The run is configured by repository secrets, in **Settings → Secrets and variables → Actions**:

| Secret | What it is |
|---|---|
| `TELEGRAM_TOKEN` | the bot that sends the digest |
| `TELEGRAM_CHAT_ID` | where to send it |
| `MIN_USD` | salary floor, $ net; below it a vacancy is marked red |
| `MY_TARGET` | the number to name first, $ net; leave unset and the comparison with the market is omitted |
| `COMPANIES` | companies already contacted, one per line: `name \| status` |
| `HOME_CITY` | home city; an office elsewhere is marked red. Unset means an office anywhere is fine |

`MIN_USD` falls back to 1000 when unset, `MY_TARGET` and `HOME_CITY` to nothing. `ENG_BAD=0` turns a demand for B2+ English from a red mark into a yellow one.

## Files

- `seen.json`, `history.json`, `analysis.json`, `last_sent.txt`: state, written by the workflow, not edited by hand.
- `digest.md`, `vacancies.html`: the day's output. Sent to Telegram and kept out of the repository.

## Running it

Outside the schedule: the **Actions** tab, "Daily vacancy digest", **Run workflow**.

Locally:

```bash
python3 generate.py                  # collect, write the files, send nothing
python3 generate.py --telegram       # and send
python3 generate.py --days 14        # a shorter window than the default 30 days
```

Locally the settings come from the environment, and `companies.txt` beside the script is read when `COMPANIES` is not set.

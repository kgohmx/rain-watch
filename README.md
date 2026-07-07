# Rain Watch SG

Compares NEA's 2-hour weather forecast against real rain-gauge readings for
Singapore, on autopilot via GitHub Actions.

## Setup

1. Files already pushed: `rain_watch.py`, `requirements.txt`,
   `.github/workflows/rain-watch.yml`
2. Go to the **Actions** tab → **Rain Watch SG** → **Run workflow** to
   trigger the first run manually (don't wait for the 30-min schedule)
3. Settings → Pages → Source: **Deploy from a branch** → Branch: **main**,
   folder: **/docs** → Save
4. Dashboard goes live at `https://<your-username>.github.io/<repo-name>/`

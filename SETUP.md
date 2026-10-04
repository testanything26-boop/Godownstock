# Godown Stock — server setup (all free, no card)

You do this once. ~15 minutes. After that, everyone opens one link on their
phone and logs in.

## Part 1 — Supabase database (5 min, free, no card)

1. Go to **supabase.com** → sign up / sign in (Google login is fine —
   company account works, nothing is blocked).
2. **New project** → name `godown-stock` → set a database password (save it
   somewhere safe) → region **Mumbai (ap-south-1)** → Create. Wait ~2 min.
3. Left menu → **SQL Editor** → **New query** → open `schema.sql` from this
   folder, paste the whole thing → **Run**. You should see "Success".
4. Left menu → **Project Settings** (gear) → **Database** → scroll to
   **Connection string** → choose **URI** → copy it. It looks like:
   `postgresql://postgres:[YOUR-PASSWORD]@db.xxxxx.supabase.co:6543/postgres`
   (Port **6543** = the pooler — best for this app.)
   **This string is a password. Never paste it in chat.**
   - Replace `[YOUR-PASSWORD]` with the database password from step 2.
   - If your password has special characters, URL-encode them:
     `@` → `%40`, `#` → `%23`, `/` → `%2F`, `?` → `%3F`, `:` → `%3A`.
     (e.g. password `ab@cd` becomes `ab%40cd`).
5. Anytime you want to peek at your data like a spreadsheet: left menu →
   **Table Editor** → open `rolls` or `history`.

## Part 2 — Render (free web server, 5 min)

1. Push this folder to a GitHub repo. Upload **every file** to the repo root —
   `app.py`, `db.py`, `index.html`, `schema.sql`, `requirements.txt`,
   `render.yaml`, `SETUP.md` (GitHub web upload can't do folders, so
   `index.html` lives at the root; the `templates/` copy is a backup).
   It already has `render.yaml`.
2. Go to **dashboard.render.com** → **New → Web Service** →
   **Build and deploy from a Git repository** → select your repo.
3. Render reads `render.yaml` automatically. Plan: **Free**.
4. Before deploying, open **Environment** and add:
   - `DATABASE_URL` → the connection string from step 4 above
     (replace `[YOUR-PASSWORD]` with the database password from step 2)
   - `ADMIN_USER` → e.g. `admin`
   - `ADMIN_PASS` → a strong password you invent
   - (`SECRET_KEY` is auto-generated; `APP_URL` comes next step)
5. **Deploy.** First build takes a few minutes.

## Part 3 — Smart QR links (2 min)

1. After deploy, copy your public URL, e.g.
   `https://godown-stock.onrender.com`
2. Render → your service → **Environment** → add `APP_URL` = that URL →
   **Save** (auto-redeploys).
3. From now on, every printed QR label encodes the full link. Anyone who
   scans a roll with their phone's normal camera app lands directly on that
   roll's page — no need to open the app first.

## Part 4 — Users

1. Open the app URL → log in as your admin.
2. **Users** tab → create one account per person (role **staff** for godown
   use, **admin** for full access).
3. Everyone opens the same link on their phone and logs in. The phone camera
   in the **Scan** tab works because the page is now https.

## Notes

- **Free-tier sleep:** Render's free plan sleeps after ~15 min of no use, and
  Supabase pauses after 7 days of no use. The first visit after a pause
  takes ~40–60 s to wake up; after that it's instant. Your data is never
  deleted.
- **Roles:** staff can add rolls, scan, log cutting, print labels, download
  reports. Only admins can create users, import CSV, or delete rolls.
- **Data:** everything lives in your Supabase Postgres database (`rolls`,
  `history`, `users`, `meta` tables). View it anytime in Table Editor.
- **Reports:** Backup tab → stock report CSV and cutting-history CSV
  (with date range). Opens in Excel.
- **Import:** Backup tab → sample CSV shows the column format; upload yours
  to bulk-inward rolls.

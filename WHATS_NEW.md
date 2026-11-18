# P2C Alert Center: What's new (one block per change)

Every change has its own number. The newest (Update 16) is on top and marked 🆕. For the full how-to of each one, see SETUP_GUIDE.md (the section name is in brackets).

## 🆕 Update 16 (18 Nov): My Tasks made simple, with a Gmail-like editor
| # | Change | What you do |
|---|--------|-------------|
| 16a | 🆕 **Task subject** field. The alert e-mail subject comes from it (one task: its subject; several: first subject + "(+2 more)"). | Nothing. |
| 16b | 🆕 **Details box like Gmail:** buttons for Bold, Italic, Underline, Strikethrough, bullets, numbers, tick box ☐ (click a box to tick it), text colour, highlight colour, clear format. No `**` or `-` needed. | Write in the box and use the buttons. |
| 16c | 🆕 **👁 Preview e-mail** button on the New task form and on Edit a task: shows the subject and the e-mail exactly as it will be sent (uses your chosen template). | Press Preview before you add. |
| 16d | 🆕 **Created (Dhaka)** column in the To do table, with date and time (AM/PM). | Nothing. |
| 16e | 🆕 **Edit a task** section: change the subject and the formatted details of any open task. | Pick the task, edit, Save. |
| 16f | 🆕 Simpler form: subject, details, three dates/time, Repeat. Repeat extras are in a closed "Repeat options" box. | Nothing. |
| 16g | 🆕 Old plain notes still work (each line becomes a bullet). The built-in task e-mail subject is updated automatically unless you changed it in Templates. | Nothing. |

## Update 15 (18 Nov): Official Ringba numbers, target caps, task e-mails, Save buttons
| # | Change | What you do |
|---|--------|-------------|
| 15a | **Connected / Converted / Paid showing 0: fixed.** The app read column names from the first call only. | Sync & Import → Backfill, to re-read old calls. |
| 15b | **Official numbers from the Ringba insights report** (`POST /v2/{accountId}/insights`). Used by cap alerts, the Reports page (new top block) and the Dashboard. | Press "Set up / upgrade the database" if shown. Open Reports → "Load official numbers from Ringba now" if days are missing. |
| 15c | **Targets with caps now found** (cap searched anywhere in the target). New "Check one target" tool. | Cap overview → Buyer targets → "Read targets from Ringba now". |
| 15d | **Task e-mails redesigned:** bold title, Work date and Alert time labels, note as bullets. | Write one point per line in the task Note box. |
| 15e | **My Tasks: no reload on every change.** The add form and the task table act only when you press their button. | Nothing. |

## Update 14: Cap overview page
- New page **🎛️ Cap overview**: publisher total caps (active-DID publishers only), global daily and monthly cap of every active target, totals per buyer. [Cap overview]

## Update 13: Only publishers with an active DID
- Publisher caps, alerts and the Publishers page only count publishers that have an active DID. Tick the box to see the others. [Only publishers with an active DID]

## Update 12: Task repeat: your own days
- Repeat on chosen weekdays, chosen month days (15, 20, last) or chosen dates, plus every N days. [Task repeat: your own days]

## Update 11: Alert Log, filters, Refresh
- Alert Log has colours (needs action / waiting / done), every table has a filter bar, every page has a Refresh button. [Alert Log: colours and filters]

## Update 10: My Tasks v2
- Alert at the task's own date and time (Dhaka, AM/PM), follow-up e-mail every 30 minutes until done, tick-done list. [My Tasks v2]

## Update 9: Publisher caps read from Ringba
- Publisher campaign caps are read automatically from Ringba. [Publisher caps per campaign]

## Update 8: New DIDs that never get a call
- Alert every 3/5/7 days (custom) until the first call, with a pause button where the AM types any number of days. [New DIDs that never get a call]

## Update 7: Answer buttons know who you are
- Personal links: managers do not type name or e-mail. [Answer buttons know who you are]

## Update 6: Copy with history for the person who answers
- The responder gets a copy with the full history of answers. [The person who answers gets a copy]

## Update 5: Answers stay in the same e-mail thread
- The answer e-mail is a reply in the original alert's thread. [Answers stay in the same email thread]

## Update 4: My Tasks (first version)
- Your own to-do list with a daily e-mail. [My Tasks]

## Updates 1 to 3: Engine and database fixes, snooze and reminders, never-alert DIDs, target and publisher caps, column order and width
- See "Update notes (Nov 2026)" in SETUP_GUIDE.md.

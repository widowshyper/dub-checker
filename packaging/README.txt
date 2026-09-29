DUB CHECKER
===========

Dub Checker looks through your anime library and tells you which shows are
missing English dub audio - and whether an English dub even exists.


STARTING IT
-----------
Double-click DubChecker.exe. Nothing is installed; everything stays in this
folder.

If Windows shows "Windows protected your PC" (SmartScreen), click
"More info" and then "Run anyway". This appears because the program isn't
signed with a paid certificate, not because anything is wrong with it.

Choose "Local files" and your anime folder (or Sonarr), then click
"Scan library" (or press F5).
The first scan reads every file; later scans only read new or changed files
and are much faster.


THE FOUR GROUPS
---------------
Needs English Audio  An English dub exists, but some episodes only have the
                     original (Japanese, Korean or Chinese) audio.
No Dub Exists        No English dub was found, so there's nothing to replace.
Fully Dubbed         Every episode already has English audio.
Check Manually       Dub Checker couldn't be sure - the Notes column says why.

Click a season's arrow or audio bar (or double-click it) to list its episodes: a tick shows which are
dual audio and which only have the original language. The coloured Audio bar
shows every episode at a glance (green dual audio, blue English only, red
original language only, amber unlabeled, grey unreadable).

Double-click an episode to see its audio tracks. If a show was
matched to the wrong AniList entry, right-click it and choose
"Wrong show? Fix the match...".

Right-click also offers "Open file location" (after a Local files scan) and,
when Sonarr is set up, "Search for replacements in Sonarr...", which asks
Sonarr to look for new releases of the episodes that only have the original
language audio. "Search Nyaa for ... dual audio" opens a search on nyaa.si in
your web browser.
Sonarr only swaps a file for a release it rates higher, so give dual-audio
releases a higher score with a custom format in Sonarr.

Your last results are kept when you close Dub Checker and shown again the
next time you open it.


MOVING TO ANOTHER PC OR A USB STICK
-----------------------------------
Copy the whole "Dub Checker" folder, including the UserData folder inside it.
Your settings, saved scan results and manual matches come with it. If your
anime folder is on the same drive as Dub Checker, it is found again even if
the drive letter changes (e.g. E: becomes F:).

Dub Checker needs to be able to write to its own folder, so don't put it in
Program Files.


UPDATING
--------
Unzip the new version, then copy your old UserData folder into the new
"Dub Checker" folder (or copy the new files over the old ones, keeping
UserData). Nothing else needs to move.


STARTING OVER
-------------
Close Dub Checker, then delete things from the UserData folder:
  config.json      your settings (includes your Sonarr API key)
  overrides.json   your manual AniList matches
  cache.db         what Dub Checker remembers about your files and AniList
  last_scan.json   the results of your last scan
  dubInfo.json     the saved copy of the MAL-Dubs list
  logs\            log files, useful when something goes wrong
  bin\             ffprobe, only downloaded if MediaInfo couldn't be used
Deleting the whole UserData folder resets everything.

In the app, "Clear results" empties the list and forgets it (it won't come
back next time). File and AniList information is kept. To forget saved
AniList answers, use Settings > General > Clear saved online info.


PRIVACY
-------
Dub Checker sends show titles to AniList (anilist.co) and downloads the
MAL-Dubs list from GitHub. Your Sonarr API key is stored in plain text in
UserData\config.json - keep that folder private.

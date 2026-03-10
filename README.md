Movie Collection App:

Desktop εφαρμογή για οργάνωση και διαχείριση προσωπικής συλλογής ταινιών με αυτόματη αναγνώριση από το TMDB (The Movie Database).

Η εφαρμογή σκανάρει τον φάκελο ταινιών, αναγνωρίζει τίτλο και έτος από το filename και κατεβάζει metadata (poster, genres, overview κλπ).


Βασικά χαρακτηριστικά
- Scan τοπικού φακέλου ταινιών
- Αυτόματη αναγνώριση μέσω TMDB API
- Αποθήκευση δεδομένων σε SQLite database
- Προβολή:
   - Poster
   - Title (Year)
   - Genres
   - Overview
- UI πλήρως παραμετροποιήσιμο μέσω ui.ini
- Debug logs για scan προβλήματα
- Custom filename parser για καθαρό matching

Δομή Project:
movie_app
│
├─ main.py                 # κύρια εφαρμογή
├─ database.py             # SQLite database layer
├─ tmdb_client.py          # επικοινωνία με TMDB API
├─ filename_parser.py      # parsing τίτλου από filenames
├─ ui_theme_icons.py       # icons και theme helpers
│
├─ ui.ini                  # UI configuration
├─ requirements.txt        # python dependencies
├─ run_app.bat             # launch script
│
└─ .gitignore

Απαιτήσεις:
- Python 3.10+
- Windows
Python libraries:
- PySide6
- requests
Εγκατάσταση:
pip install -r requirements.txt

Εκτέλεση εφαρμογής:
Ο πιο απλός τρόπος:
run_app.bat
ή από terminal:
python main.py

Movie Library Structure:
Η εφαρμογή είναι σχεδιασμένη να δουλεύει με δομή φακέλων τύπου:

Movies
│
├─ Gladiator (2000) [1080p]
│  └─ Gladiator (2000) [1080p].mkv
│
├─ The Matrix (1999) [1080p]
│  └─ The Matrix (1999) [1080p].mp4
│
└─ Interstellar (2014) [1080p]
   └─ Interstellar (2014) [1080p].mkv

Configuration:
Όλες οι ρυθμίσεις UI βρίσκονται στο:
ui.ini
Από εκεί ελέγχονται:
- fonts
- colors
- toolbar
- poster grid
- details pane

Debugging:
Κατά το scan δημιουργούνται debug αρχεία όπως:
scan_debug.csv
run_stdout.txt
run_error.txt
Αυτά δεν αποθηκεύονται στο GitHub μέσω .gitignore.

Μελλοντικές βελτιώσεις:
Planned features:
- media metadata extraction (ffprobe)
- codec / audio / subtitles info
- quality scoring
- duplicate detection
- better TMDB ranking

License:
Personal project.
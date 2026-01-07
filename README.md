# 🎬 Letterboxd Recommender

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0+-green.svg)](https://flask.palletsprojects.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Live Demo](https://img.shields.io/badge/Demo-Live-success.svg)](https://letterboxd-recommender-production.up.railway.app/)

**Letterboxd Recommender** is an intelligent movie recommendation system that analyzes your Letterboxd profile to deliver personalized film suggestions. By combining web scraping, TMDB metadata enrichment, and streaming availability lookups, it helps you discover your next favorite movie based on what you already love.

🔗 **Live Demo:** [letterboxd-recommender-production.up.railway.app](https://letterboxd-recommender-production.up.railway.app/)

---

## 📖 Table of Contents

- [Features](#-features)
- [How It Works](#-how-it-works)
- [Tech Stack](#-tech-stack)
- [Prerequisites](#-prerequisites)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [API Reference](#-api-reference)
- [Project Structure](#-project-structure)
- [Contributing](#-contributing)
- [License](#-license)
- [Acknowledgments](#-acknowledgments)

---

## ✨ Features

- 🎯 **Personalized Recommendations**: Analyzes your highly-rated films (4+ stars) to suggest similar movies
- 🌍 **Streaming Availability**: Shows where each recommendation is available to stream in your country
- 🧠 **Smart Preference Analysis**: Identifies your favorite genres, directors, and decades
- ⚡ **Real-time Updates**: Server-Sent Events (SSE) for live progress updates and streaming recommendations
- 💾 **Intelligent Caching**: Redis support with automatic fallback to in-memory cache
- 🔄 **Concurrent Processing**: Multi-threaded enrichment for fast recommendation generation
- 📊 **Export Functionality**: Download recommendations to Excel for offline reference
- 🎨 **Clean UI**: Framework-free vanilla JavaScript frontend with dark mode
- 🔍 **Deduplication**: Ensures you never see movies you've already watched
- 🎭 **Multi-Country Support**: Streaming availability for 13+ countries

---

## 🎬 How It Works

The recommendation engine follows a sophisticated multi-step pipeline:

1. **Profile Scraping**: Fetches all films from a user's Letterboxd profile (including unrated entries to track what you've already seen)
2. **TMDB Enrichment**: Augments each film with metadata from The Movie Database (year, genres, director, runtime, poster, rating)
3. **Preference Analysis**: Identifies patterns in your viewing history - favorite genres, directors, and decades
4. **Similar Film Discovery**: For every 4+ star film, queries TMDB for similar titles
5. **Intelligent Filtering**: Removes already-watched films, applies minimum rating threshold (default: 7.0), and deduplicates
6. **Streaming Lookup**: Checks availability across multiple platforms for your selected country
7. **Real-time Delivery**: Streams recommendations to the frontend as they're discovered

---

## 🛠️ Tech Stack

### Backend
- **[Python 3.11+](https://www.python.org/)**: Core application language
- **[Flask](https://flask.palletsprojects.com/)**: Lightweight web framework with CORS support
- **[BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/)**: HTML parsing for Letterboxd scraping
- **[Requests](https://requests.readthedocs.io/)**: HTTP library with retry logic
- **[Gunicorn](https://gunicorn.org/)**: Production WSGI server

### External APIs
- **[TMDB API](https://www.themoviedb.org/documentation/api)**: Movie metadata and similar film recommendations
- **[SimpleJustWatch](https://github.com/Electronic-Mango/simple-justwatch-python-api)**: Streaming availability lookup

### Caching & Performance
- **[Redis](https://redis.io/)**: Optional distributed cache (with automatic fallback)
- **[cachetools](https://pypi.org/project/cachetools/)**: In-memory TTL cache
- **ThreadPoolExecutor**: Concurrent processing for API calls

### Frontend
- **Vanilla JavaScript**: No framework dependencies
- **Server-Sent Events (SSE)**: Real-time progress updates
- **LocalStorage**: Client-side caching

### Deployment
- **[Railway](https://railway.app/)**: Cloud platform hosting
- **[Procfile](https://devcenter.heroku.com/articles/procfile)**: Process configuration

---

## 📋 Prerequisites

Before installing, ensure you have:

- **Python 3.11 or higher** ([Download](https://www.python.org/downloads/))
- **pip** (Python package manager, included with Python)
- **TMDB API Key** ([Get one free here](https://www.themoviedb.org/settings/api))
- **(Optional) Redis** for distributed caching ([Installation guide](https://redis.io/docs/getting-started/installation/))

---

## 🚀 Installation

### 1. Clone the Repository

```bash
git clone https://github.com/based-on-what/letterboxd-recommender.git
cd letterboxd-recommender
```

### 2. Create a Virtual Environment (Recommended)

```bash
# Create virtual environment
python -m venv venv

# Activate on macOS/Linux
source venv/bin/activate

# Activate on Windows
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

The following packages will be installed:
- `flask` - Web framework
- `flask-cors` - Cross-origin resource sharing
- `requests` - HTTP library
- `beautifulsoup4` - HTML parser
- `cachetools` - In-memory caching
- `simple-justwatch-python-api` - Streaming availability
- `redis` - Redis client (optional)
- `gunicorn` - WSGI server
- `python-dotenv` - Environment variable management

---

## ⚙️ Configuration

### Environment Variables

Create a `.env` file in the project root:

```bash
# Required
TMDB_KEY=your_tmdb_api_key_here

# Optional - Redis Configuration
REDIS_URL=redis://localhost:6379

# Optional - Application Settings
PORT=8080
FLASK_ENV=development
MIN_RECOMMEND_RATING=7.0
MAX_SCRAPE_PAGES=50
DEFAULT_MAX_FILMS=30
DEFAULT_LIMIT_RECS=60
```

#### Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TMDB_KEY` | **Yes** | - | Your TMDB API key for movie data |
| `REDIS_URL` | No | - | Redis connection string (falls back to in-memory cache if not set) |
| `PORT` | No | `8080` | Port for Flask/Gunicorn server |
| `MIN_RECOMMEND_RATING` | No | `7.0` | Minimum TMDB rating threshold for recommendations |
| `MAX_SCRAPE_PAGES` | No | `50` | Maximum Letterboxd pages to scrape per user |
| `DEFAULT_MAX_FILMS` | No | `30` | Maximum films to enrich for preference analysis |
| `DEFAULT_LIMIT_RECS` | No | `60` | Maximum recommendations to return |
| `FLASK_ENV` | No | `production` | Set to `development` for debug mode |

---

## 💻 Usage

### Running Locally

#### Development Mode (with auto-reload)

```bash
FLASK_ENV=development python main.py
```

#### Production Mode

```bash
gunicorn -c gunicorn.conf.py main:app
```

Or use the Railway Procfile configuration:

```bash
gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

### Using the Application

1. **Open your browser** and navigate to `http://localhost:8080`

2. **Enter a Letterboxd username** (e.g., `karsten`, `davidehrlich`, `debracardoso`)

3. **Select your country** for streaming availability (optional)

4. **Click "Get Recommendations"** - the app will:
   - Scrape the user's Letterboxd profile
   - Analyze viewing preferences
   - Generate personalized recommendations
   - Display streaming availability

5. **View results** at `http://localhost:8080/<username>`

6. **Export to Excel** using the download button (optional)

### Example Workflow

```bash
# Start the server
python main.py

# In your browser, visit:
http://localhost:8080

# Enter username: "karsten"
# Select country: "United States"
# Click: "Get Recommendations"

# Results appear at:
http://localhost:8080/karsten
```

---

## 📡 API Reference

### Health Check

```http
GET /_health
```

Returns a basic health status for monitoring.

**Response:**
```json
{
  "status": "ok"
}
```

---

### Get Page Count

```http
POST /api/get_pages
```

Returns the total number of film pages for a Letterboxd user.

**Request Body:**
```json
{
  "username": "karsten"
}
```

**Response:**
```json
{
  "pages": 42
}
```

---

### Generate Recommendations

```http
POST /api/recommend
```

Generates personalized movie recommendations based on a user's Letterboxd profile.

**Request Body:**
```json
{
  "username": "karsten",
  "country": "US",
  "include_streaming": true
}
```

**Parameters:**
- `username` (string, required): Letterboxd username
- `country` (string, optional): ISO country code (default: "CL")
- `include_streaming` (boolean, optional): Include streaming availability (default: true)

**Response:**
```json
{
  "username": "karsten",
  "country_name": "United States",
  "pages": 42,
  "preferences": {
    "genres": ["Drama", "Thriller", "Science Fiction"],
    "directors": ["Denis Villeneuve", "Christopher Nolan", "Bong Joon-ho"],
    "decades": ["2010s", "2020s", "2000s"]
  },
  "recommendations": [
    {
      "tmdb_id": 12345,
      "title": "Blade Runner 2049",
      "original_title": "Blade Runner 2049",
      "year": "2017",
      "director": "Denis Villeneuve",
      "genres": ["Science Fiction", "Drama"],
      "poster": "https://image.tmdb.org/t/p/w500/...",
      "rating_tmdb": 7.9,
      "runtime": 164,
      "streaming": ["Prime Video", "HBO Max"],
      "reason": "Since you liked Arrival"
    }
  ]
}
```

---

### Real-Time Streaming Endpoints (SSE)

#### Logs Stream
```http
GET /api/logs-stream
```

Server-Sent Events endpoint for real-time log updates.

#### Recommendations Stream
```http
GET /api/recommendations-stream
```

Server-Sent Events endpoint for streaming recommendations as they're found.

---

### Static Routes

- `GET /` - Landing page with input form
- `GET /<username>` - Results page displaying recommendations

---

## 📁 Project Structure

```
letterboxd-recommender/
├── main.py                 # Core Flask application and recommendation engine
├── app.py                  # WSGI entry point
├── index.html              # Landing page UI
├── results.html            # Recommendations display page
├── requirements.txt        # Python dependencies
├── runtime.txt             # Python version specification
├── gunicorn.conf.py        # Gunicorn server configuration
├── Procfile                # Railway/Heroku deployment config
├── .env                    # Environment variables (not in repo)
├── .gitignore              # Git ignore rules
└── README.md               # This file
```

### Key Components

- **`MovieRecommender` class**: Core recommendation engine with methods for:
  - Scraping Letterboxd profiles (`get_all_rated_films`)
  - TMDB API integration (`get_tmdb_details`, `get_tmdb_details_by_id`)
  - Preference analysis (`analyze_preferences`)
  - Recommendation generation (`get_recommendations`)
  - Streaming lookup (`get_streaming`, `get_streaming_by_tmdb`)

- **`Cache` class**: Abstraction layer supporting Redis and in-memory caching

- **`RateLimiter` class**: Thread-safe rate limiting for external APIs

- **Flask routes**: RESTful endpoints for health checks, page counting, and recommendations

---

## 🤝 Contributing

Contributions are welcome! Here's how you can help:

### Reporting Issues

1. Check [existing issues](https://github.com/based-on-what/letterboxd-recommender/issues) first
2. Create a new issue with:
   - Clear title and description
   - Steps to reproduce (if bug)
   - Expected vs. actual behavior
   - Environment details (OS, Python version)

### Pull Requests

1. **Fork the repository**
   ```bash
   git clone https://github.com/your-username/letterboxd-recommender.git
   ```

2. **Create a feature branch**
   ```bash
   git checkout -b feature/amazing-feature
   ```

3. **Make your changes**
   - Follow PEP 8 style guidelines
   - Add docstrings to new functions/classes
   - Update documentation as needed

4. **Test your changes**
   ```bash
   # Run the application locally
   python main.py
   
   # Test with different usernames and countries
   ```

5. **Commit your changes**
   ```bash
   git commit -m "Add: amazing new feature"
   ```
   
   Use conventional commits:
   - `Add:` for new features
   - `Fix:` for bug fixes
   - `Update:` for changes to existing features
   - `Docs:` for documentation changes

6. **Push to your fork**
   ```bash
   git push origin feature/amazing-feature
   ```

7. **Open a Pull Request**
   - Describe your changes clearly
   - Reference any related issues
   - Wait for review and feedback

### Development Guidelines

- **Code Style**: Follow [PEP 8](https://pep8.org/) conventions
- **Documentation**: Add docstrings for all public functions/classes
- **Error Handling**: Use try-except blocks with meaningful logging
- **Rate Limiting**: Respect API rate limits for TMDB and Letterboxd
- **Caching**: Leverage the cache system to minimize redundant API calls

---

## 📄 License

This project is licensed under the **MIT License** - see below for details:

```
MIT License

Copyright (c) 2024 Letterboxd Recommender Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 🙏 Acknowledgments

This project wouldn't be possible without:

- **[Letterboxd](https://letterboxd.com/)** - The social network for film lovers that inspired this project
- **[The Movie Database (TMDB)](https://www.themoviedb.org/)** - Comprehensive movie metadata and API
- **[JustWatch](https://www.justwatch.com/)** - Streaming availability data via SimpleJustWatch
- **[Railway](https://railway.app/)** - Hassle-free deployment and hosting
- **Open Source Contributors** - Everyone who has contributed code, reported bugs, or suggested features

### Special Thanks

- The Flask community for excellent documentation
- BeautifulSoup maintainers for robust HTML parsing
- All users who have tested and provided feedback

---

## 📧 Contact & Support

- **GitHub Issues**: [Report bugs or request features](https://github.com/based-on-what/letterboxd-recommender/issues)
- **Live Demo**: [Try it out](https://letterboxd-recommender-production.up.railway.app/)
- **Repository**: [Source code](https://github.com/based-on-what/letterboxd-recommender)

### Maintainers

This project is maintained by the community. If you'd like to become a maintainer, please reach out via GitHub issues or pull requests.

---

<div align="center">

**Made with ❤️ for movie lovers everywhere**

⭐ **Star this repo** if you find it useful!

[Report Bug](https://github.com/based-on-what/letterboxd-recommender/issues) · [Request Feature](https://github.com/based-on-what/letterboxd-recommender/issues) · [Live Demo](https://letterboxd-recommender-production.up.railway.app/)

</div>

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from collections import Counter
from bs4 import BeautifulSoup
import time
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from simplejustwatchapi import justwatch as sjw

app = Flask(__name__)
CORS(app)

class MovieRecommender:
    def __init__(self, country='CL', max_workers=10):
        self.letterboxd_base = "https://letterboxd.com"
        self.tmdb_base = "https://api.themoviedb.org/3"
        self.tmdb_key = os.getenv('TMDB_KEY')
        self.country = country.upper()
        self.max_workers = max_workers
        self.print_lock = Lock()
        self.country_names = {
            'CL': 'Chile', 'AR': 'Argentina', 'MX': 'Mexico', 'US': 'United States',
            'ES': 'Spain', 'BR': 'Brazil', 'CO': 'Colombia', 'PE': 'Peru', 'UY': 'Uruguay',
            'IT': 'Italy', 'FR': 'France', 'DE': 'Germany', 'GB': 'United Kingdom'
        }

    def get_country_name(self):
        return self.country_names.get(self.country, self.country)

    def _safe_get(self, url, params=None, headers=None, max_retries=2):
        for attempt in range(max_retries + 1):
            try:
                r = requests.get(url, params=params, headers=headers, timeout=15)
                if r.status_code == 200: return r
                time.sleep(0.5)
            except: continue
        return None

    def get_page_count(self, username):
        """Retrieves only the page count for estimated time calculation"""
        base_url = f"{self.letterboxd_base}/{username}/films/"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = self._safe_get(base_url, headers=headers)
        if not response: return 0
        soup = BeautifulSoup(response.text, 'html.parser')
        pagination = soup.find_all('li', class_='paginate-page')
        if not pagination: return 1
        try:
            pages = [int(p.get_text()) for p in pagination if p.get_text().isdigit()]
            return max(pages) if pages else 1
        except: return 1

    def get_all_rated_films(self, username):
        films = []
        rating_map = {f'rated-{i}': i/2.0 for i in range(1, 11)}
        base_url = f"{self.letterboxd_base}/{username}/films/"
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        num_pages = self.get_page_count(username)
        if num_pages == 0: return [], 0

        def scrape_page(page):
            url = f"{base_url}page/{page}/" if page > 1 else base_url
            r = self._safe_get(url, headers=headers)
            if not r: return []
            soup = BeautifulSoup(r.text, 'html.parser')
            items = soup.find_all('li', class_='poster-container') or soup.find_all('li', class_='griditem')
            page_films = []
            for item in items:
                try:
                    name = item.find('img', alt=True).get('alt')
                    rating = 0.0
                    viewingdata = item.find('p', class_='poster-viewingdata')
                    if viewingdata:
                        r_span = viewingdata.find('span', class_='rating')
                        if r_span:
                            for cls in r_span.get('class', []):
                                if cls in rating_map: rating = rating_map[cls]; break
                    if name: page_films.append({'title': name, 'rating': rating, 'year': None})
                except: continue
            return page_films

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(scrape_page, p) for p in range(1, num_pages + 1)]
            for f in as_completed(futures): films.extend(f.result())
        
        return [f for f in films if f['rating'] > 0], num_pages

    def get_tmdb_details(self, title):
        try:
            params = {'api_key': self.tmdb_key, 'query': title, 'language': 'en-US'}
            resp = self._safe_get(f"{self.tmdb_base}/search/movie", params=params)
            if not resp or not resp.json()['results']: return None
            movie_id = resp.json()['results'][0]['id']
            
            d_resp = self._safe_get(f"{self.tmdb_base}/movie/{movie_id}", params={'api_key': self.tmdb_key, 'language': 'en-US'})
            c_resp = self._safe_get(f"{self.tmdb_base}/movie/{movie_id}/credits", params={'api_key': self.tmdb_key})
            
            det = d_resp.json()
            cre = c_resp.json()
            director = next((c['name'] for c in cre.get('crew', []) if c['job'] == 'Director'), "Unknown")
            
            return {
                'tmdb_id': movie_id,
                'title': det.get('title'),
                'year': det.get('release_date', '0000')[:4],
                'director': director,
                'genres': [g['name'] for g in det.get('genres', [])],
                'poster': f"https://image.tmdb.org/t/p/w500{det['poster_path']}" if det.get('poster_path') else None,
                'rating_tmdb': round(det.get('vote_average', 0), 1),
                'runtime': det.get('runtime', 0)
            }
        except: return None

    def analyze_preferences(self, enriched_films):
        """Analyzes user preferences"""
        genres = []
        directors = []
        decades = []
        
        for film in enriched_films:
            if film.get('genres'):
                genres.extend(film['genres'])
            if film.get('director') and film['director'] != "Unknown":
                directors.append(film['director'])
            if film.get('year'):
                try:
                    decade = (int(film['year']) // 10) * 10
                    decades.append(decade)
                except: pass
        
        genre_counts = Counter(genres).most_common(3)
        director_counts = Counter(directors).most_common(3)
        decade_counts = Counter(decades).most_common(Decade_counts)
        
        return {
            'genres': [g[0] for g in genre_counts] if genre_counts else ['N/A'],
            'directors': [d[0] for d in director_counts] if director_counts else ['N/A'],
            'decades': [f"{d[0]}s" for d in decade_counts] if decade_counts else []
        }

    def get_recommendations(self, enriched_films, count=60):
        seen = {f['title'].lower() for f in enriched_films}
        recs = []
        top_films = sorted(enriched_films, key=lambda x: x['user_rating'], reverse=True)[:10]

        def process_similar(film):
            url = f"{self.tmdb_base}/movie/{film['tmdb_id']}/similar"
            resp = self._safe_get(url, params={'api_key': self.tmdb_key, 'language': 'en-US'})
            if not resp: return []
            local = []
            for m in resp.json().get('results', [])[:5]:
                if m['title'].lower() not in seen:
                    det = self.get_tmdb_details(m['title'])
                    if det:
                        det['reason'] = f"Since you liked {film['title']}"
                        local.append(det)
            return local

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_similar, f) for f in top_films]
            for f in as_completed(futures): recs.extend(f.result())
        return recs[:count]

    def get_streaming(self, title, year):
        try:
            entries = sjw.search(title, country=self.country, language='en', count=1, best_only=True)
            if entries:
                providers = [o.package.name for o in entries[0].offers if o.package]
                return sorted(list(set(providers)))
        except: pass
        return []

@app.route('/api/get_pages', methods=['POST'])
def get_pages():
    username = request.json.get('username')
    count = MovieRecommender().get_page_count(username)
    return jsonify({'pages': count})

@app.route('/api/recommend', methods=['POST'])
def recommend():
    data = request.json
    username = data.get('username')
    country = data.get('country', 'CL')
    
    rec_sys = MovieRecommender(country=country)
    user_films, pages = rec_sys.get_all_rated_films(username)
    
    if not user_films: return jsonify({'error': 'No movies found'}), 404

    enriched = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        def task(f):
            d = rec_sys.get_tmdb_details(f['title'])
            if d: d['user_rating'] = f['rating']; return d
        futures = [executor.submit(task, f) for f in user_films[:30]]
        for f in as_completed(futures):
            res = f.result()
            if res: enriched.append(res)

    preferences = rec_sys.analyze_preferences(enriched)
    recommendations = rec_sys.get_recommendations(enriched)
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(rec_sys.get_streaming, m['title'], m['year']): m for m in recommendations}
        for f in as_completed(futures):
            futures[f]['streaming'] = f.result()

    return jsonify({
        'username': username,
        'country_name': rec_sys.get_country_name(),
        'recommendations': recommendations,
        'preferences': preferences,
        'pages': pages
    })

if __name__ == '__main__':
    # Use environment port for production
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
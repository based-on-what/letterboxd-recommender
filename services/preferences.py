"""
services/preferences.py — Pure preference analysis with no I/O.
"""

from collections import Counter


def analyze_preferences(enriched_films: list) -> dict:
    genres, directors, decades = [], [], []

    for film in enriched_films:
        if film.get('genres'):
            genres.extend(film['genres'])
        if film.get('director') and film['director'] != "Unknown":
            directors.append(film['director'])
        if film.get('year'):
            try:
                decades.append((int(film['year']) // 10) * 10)
            except (ValueError, TypeError):
                pass

    return {
        'genres': [g for g, _ in Counter(genres).most_common(3)],
        'directors': [d for d, _ in Counter(directors).most_common(3)],
        'decades': [f"{d}s" for d, _ in Counter(decades).most_common(3)],
    }

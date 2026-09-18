# scraper

Findet Inserate auf Kleinanzeigen, Immobilienscout24 und Immowelt und speichert
ihre Detailseiten roh in PostgreSQL. Läuft auf GitHub Actions.

```
Find  → Suchseiten durchgehen, neue Inserat-IDs anlegen
Scrape → Detailseiten holen, HTML und JSON speichern
```

Alles Weitere — Strukturieren, Geodaten, Bewertung — passiert in anderen
Diensten der Fixfolio-Pipeline.

- Aufbau und Konventionen: [CLAUDE.md](CLAUDE.md)
- Einrichten und Betrieb: [workflow.md](workflow.md)

Zugangsdaten liegen nicht im Repo. `.env.template` zeigt, was gesetzt sein muss.

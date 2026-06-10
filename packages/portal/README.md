# PayProbe Portal

Angular 17+ management UI for PayProbe.

## Features

- **Dashboard** — run status overview, component health grid
- **Run Monitor** — live WebSocket feed, phase progress, step-by-step trace
- **Reports** — report viewer with baseline diff highlighting
- **Test Constructor** — visual ngx-graph workflow editor
- **Environments** — manage environment configurations

## Development

```bash
npm install
npm start          # dev server on http://localhost:4200
npm run build      # production build
npm run lint       # ESLint + Prettier check
npm test           # Karma unit tests
```

## Tech Stack

- Angular 17 (standalone components)
- Angular Material
- RxJS WebSocketSubject for live updates
- ngx-graph for test constructor canvas
- API client auto-generated from FastAPI OpenAPI spec

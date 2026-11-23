.PHONY: install frontend api test compose-up compose-down change

install:
	npm install --prefix frontend --no-audit --no-fund

frontend:
	npm run dev --prefix frontend

api:
	python3 backend/app.py

test:
	cd backend && python3 -m unittest discover -s tests -v

compose-up:
	docker compose up --build

compose-down:
	docker compose down --remove-orphans

change:
	./scripts/apply-demo-change.sh

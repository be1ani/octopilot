FROM node:20-alpine AS build

WORKDIR /app
COPY orchestrator/frontend/package.json /app/package.json
COPY orchestrator/frontend/package-lock.json /app/package-lock.json
RUN npm ci

COPY orchestrator/frontend/index.html /app/index.html
COPY orchestrator/frontend/vite.config.js /app/vite.config.js
COPY orchestrator/frontend/public /app/public
COPY orchestrator/frontend/src /app/src

RUN npm run build

FROM nginx:1.27-alpine
COPY orchestrator/frontend/nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080


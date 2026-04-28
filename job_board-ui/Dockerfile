FROM node:20-alpine AS build

WORKDIR /app
COPY job_board-ui/package.json /app/package.json
RUN npm install

COPY job_board-ui/index.html /app/index.html
COPY job_board-ui/vite.config.js /app/vite.config.js
COPY job_board-ui/src /app/src

RUN npm run build

FROM nginx:1.27-alpine
COPY job_board-ui/nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080


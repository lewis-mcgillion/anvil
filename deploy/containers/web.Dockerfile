FROM node:22-alpine AS build
WORKDIR /app
COPY api /app/api
COPY web/package.json web/package-lock.json /app/web/
RUN cd /app/web && npm ci
COPY web /app/web
RUN cd /app/web && npm run build

FROM nginx:1.27-alpine
COPY deploy/containers/web.nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/web/dist /usr/share/nginx/html
EXPOSE 8080

# Multi-stage: build the Angular portal, serve it from nginx.
# The compose files mount the PayProbe nginx config (with /api routing)
# over the default one.

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist/payprobe-portal/browser /usr/share/nginx/html
EXPOSE 80

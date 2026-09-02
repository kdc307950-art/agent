# syntax=docker/dockerfile:1
# 前端镜像：构建 React 静态文件，由 Nginx 托管。
# 明确不写入任何开发令牌；令牌由用户在浏览器 sessionStorage 粘贴，运行时取自请求头。
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

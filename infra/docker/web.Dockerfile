ARG WEB_BASE_IMAGE=docker.1panel.live/library/node:24-alpine
FROM ${WEB_BASE_IMAGE}

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

COPY package.json /app/package.json
COPY package-lock.json /app/package-lock.json
COPY apps/web/package.json /app/apps/web/package.json

RUN npm config set registry ${NPM_REGISTRY} \
    && npm install --registry ${NPM_REGISTRY}

COPY . /app

CMD ["npm", "--workspace", "apps/web", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

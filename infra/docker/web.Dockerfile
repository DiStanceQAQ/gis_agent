FROM node:24-alpine

WORKDIR /app

COPY package.json /app/package.json
COPY apps/web/package.json /app/apps/web/package.json

RUN npm install

COPY . /app

CMD ["npm", "--workspace", "apps/web", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]


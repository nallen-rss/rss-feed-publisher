import { Readability } from "@mozilla/readability";
import { JSDOM } from "jsdom";

let input = "";

process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) {
  input += chunk;
}

const { url, html } = JSON.parse(input);
const dom = new JSDOM(html, { url });
const article = new Readability(dom.window.document).parse();

process.stdout.write(JSON.stringify({
  title: article?.title || "",
  byline: article?.byline || "",
  excerpt: article?.excerpt || "",
  content: article?.content || "",
}));

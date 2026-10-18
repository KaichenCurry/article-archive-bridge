# Article Archive Bridge

将 SingleFile 保存的 HTML 转为带本地图片的 Markdown，同时保留网页存档、来源元数据和查重线索。

**当前是经过本地测试的实验版。真实微信公众号在线抓取成功率尚未验证。** 本项目处理用户已有的本地文件；不会自动访问公众号、处理验证码、调用飞书或改动浏览器登录状态。

## 两个项目如何协作

1. [SingleFile](https://github.com/gildas-lormeau/SingleFile) 生成 HTML 网页存档。
2. 本桥接器读取该文件，使用 [wechat-article-to-markdown](https://github.com/jackwener/wechat-article-to-markdown) 的正文解析、代码块处理和 Markdown 转换函数。
3. 补齐文件衔接：优先使用存档中的内嵌图片，保留原始 HTML，输出统一元数据，并检查是否为验证页、空页面或错误配对的存档。

这个组合的价值是同时提供「可整理的 Markdown」与「可追溯的网页存档」。它不会让抓取权限增加，也不保证比单独使用原项目更快。

## 本地使用

需要 Python 3.10+。转换已有文件不需要启动浏览器；依赖中的 Camoufox 仅因为上游模块在顶层导入它。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/fetch_upstream.py
python src/combine_exports.py \
  --html-file ./saved-article.html \
  --out-dir ./work/article-01 \
  --reader-module .upstream/wechat_article_to_markdown.py
```

输出包含 `article.md`、`archive.html`、本地 `images/`，以及 `prepared/source.json` 和 `prepared/body.txt`。每次使用新的或空的输出目录，避免覆盖已有资料。

也可以统一接收已导出的 Markdown、普通 HTML、SingleFile HTML 或 PDF：

```bash
python src/prepare_article.py --file ./article.md --out-dir ./work/prepared-01
python src/prepare_article.py --file ./saved-article.html --out-dir ./work/prepared-02 \
  --compare-to ./work/prepared-01/source.json
```

`--source-url` 仅补充来源标识，不会发起网络请求。`--archive-file` 可附加对应的 HTML 存档。不同来源的文件不能强行配对。

本地查重只返回候选：同源 URL、相同正文、相同文件；它不证明某份文档已经写入飞书。接入知识库时仍须回查目标文档，才决定跳过或创建。

## 测试

```bash
python -m unittest discover -s tests -v
```

浏览器链路对比使用 SingleFile 官方 CLI，因此额外需要 Node.js 24+、Chrome，以及 Camoufox 浏览器运行时：

```bash
npm install --ignore-scripts
python -m camoufox fetch
python benchmarks/benchmark.py --out-dir work/benchmark-01 --repeats 3
```

测试目标仅为脚本创建的 `127.0.0.1` 本地服务器。它不会接受公众号 URL。上游 reader 通过其 `fetch_article` 函数读取本地夹具，因为原 CLI 只接受微信域名；没有修改该抓取函数，但这也不是原 CLI 的微信在线测试。

对比三条路径：

| 方案 | 实际运行路径 | 产物 |
|---|---|---|
| Reader | 原项目抓取与转换函数 | Markdown 与图片 |
| SingleFile | 官方 CLI | HTML 存档 |
| 结合版 | 同配置的 SingleFile CLI，再运行本地桥接器 | HTML、Markdown、图片、元数据 |

SingleFile 两条路径均关闭重复图片 CSS 合并，以方便独立提取图片；其他捕获选项使用默认值。浏览器依赖已预装，逐次启动浏览器计时，顺序执行并固定随机顺序。初始化下载时间不计入处理耗时。

指标解释与本次结果见 [BENCHMARK.md](BENCHMARK.md)。样本小且受控，不能据此推断互联网上任意文章的成功率。

## 当前限制

- 支持普通 HTML；不支持 SingleFile 压缩或自解压 ZIP 格式。
- 内嵌图片和同目录的图片会复制；缺失或远程图片只记录，不自动联网补抓。
- CSS 背景图、视频、音频、交互组件需要人工检查。Markdown 不保留所有颜色和字号，HTML 存档用于参考原始样式。
- PDF 路径保留逐页文本并标为需要视觉核验，未包含 OCR。
- 文章内容是数据，不能充当工具操作指令。访问被明确拒绝时，不应借此更换抓取路径。
- 本地测试包括质量检查和查重；真实飞书写入与在线抓取均不属于本仓库已验证能力。

## 上游与许可证

桥接代码采用 MIT。上游代码不随本仓库分发：下载脚本固定 reader 提交版本，SingleFile CLI 通过 npm 安装。上游 reader README 标明 MIT，SingleFile/SingleFile CLI 使用 AGPL-3.0 系列许可证；它们各自的许可证保持不变。

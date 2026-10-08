# 施工现场安全帽智能检测系统

这是一个面向施工现场的视频安全巡检系统。系统支持本地视频、海康 ISAPI 历史录像和 RTSP 实时流，使用 YOLO 检测 `person / head / helmet`，通过头部与安全帽框的空间关系判断违规，再使用 OSNet 对重复出现的违规人员进行合并。

## 核心流程

```text
本地视频 / ISAPI 录像 / RTSP 实时流
                    ↓
              抽取待分析帧
                    ↓
       YOLO 检测 person/head/helmet
                    ↓
          业务规则判断未戴安全帽
                    ↓
             截取违规人员区域
                    ↓
          OSNet 提取人物外观特征
                    ↓
            相似度匹配与人物合并
                    ↓
       保存任务结果 / 短信告警 / 前端展示
```

## 三种视频来源

- **本地上传**：保存上传文件，后台线程分析完整视频。
- **ISAPI 历史录像**：按设备、通道和时间范围下载录像，再进入离线分析流程。
- **RTSP 实时流**：为监控任务创建长期运行线程，支持实时预览、主动停止和断线重连。

## 项目结构

```text
hat/
├── api/
│   └── main.py                  # FastAPI 路由和请求入口
├── services/
│   ├── analysis/                # YOLO检测、OSNet特征、人物合并
│   ├── tasks/                   # 离线任务和RTSP任务编排
│   ├── persistence/             # MySQL、数据模型、会话结果
│   ├── integrations/            # ISAPI和短信服务
│   ├── auth/                    # 登录令牌和密码校验
│   ├── common/                  # 图像、时间等通用工具
│   └── config.py                # 统一项目路径
├── frontend/                    # Vue 3 + Element Plus 前端
├── models/                      # YOLO模型结构兼容代码
├── utils/                       # YOLO推理依赖的工具代码
├── weights/                     # YOLO和OSNet模型权重
├── sql/                         # 数据库初始化与迁移脚本
├── tools/                       # 独立测试与本地联调工具
│   ├── demo_video_inference.py  # 单视频模型测试
│   └── isapi_mock_server.py     # 本地ISAPI模拟服务
├── run_api.py                   # 后端启动入口
└── requirements.txt
```

更详细的文件职责和三条调用链见 [代码地图](docs/CODE_MAP.md)。

## 快速启动

### 1. 创建环境并安装依赖

推荐 Python 3.10：

```bash
conda create -n hat python=3.10 -y
conda activate hat
pip install -r requirements.txt
```

`requirements.txt` 默认安装 CPU 版 ONNX Runtime。使用 NVIDIA GPU 时，可根据本机 CUDA 环境替换为 `onnxruntime-gpu`，PyTorch 也应安装对应 CUDA 版本。

### 2. 准备模型

确认下列文件存在：

```text
weights/helmet_head_person_l.pt
weights/osnet_ain_msmt17_dynamic_ir9.onnx
```

### 3. 配置环境变量

复制示例配置：

```powershell
Copy-Item .env.example .env
```

至少修改 `.env` 中的 `AUTH_SECRET` 和 `DATABASE_URL`。真实密码、短信密钥和 RTSP 地址不要提交到 Git。

### 4. 初始化MySQL

依次执行：

```text
sql/00_create_database.sql
sql/01_users.sql
sql/02_devices.sql
sql/03_monitor_sessions.sql
```

`sql/migrations/` 只用于升级旧数据库，全新数据库无需执行。

### 5. 启动后端

```bash
python run_api.py
```

后端地址：`http://127.0.0.1:18765`  
接口文档：`http://127.0.0.1:18765/docs`

### 6. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端地址：`http://127.0.0.1:5173`。

生产构建：

```bash
npm run build
```

构建结果位于 `frontend/dist`，由 FastAPI 静态托管。

## 独立测试工具

不启动完整系统，只测试安全帽模型：

```bash
python tools/demo_video_inference.py "输入视频.mp4" "输出结果.mp4" --frame-stride 25
```

没有真实海康设备时，可用本地模拟服务联调 ISAPI：

```bash
python tools/isapi_mock_server.py --video-ch1 "测试视频1.mp4" --video-ch2 "测试视频2.mp4"
```

`tools/` 中的文件不是正式系统启动入口；正式后端统一通过 `python run_api.py` 启动。

## 主要技术栈

- 前端：Vue 3、TypeScript、Element Plus、Axios
- 后端：FastAPI、Pydantic、SQLAlchemy、MySQL
- 视频：OpenCV、Decord、FFmpeg、RTSP、海康 ISAPI
- AI：PyTorch YOLO、ONNX Runtime OSNet
- 并发：Python 后台线程和线程安全任务状态

## 说明

- `inference/`、`frontend/dist/`、`frontend/node_modules/` 和 Python缓存均为运行时生成内容，不纳入版本控制。
- `models/` 和 `utils/` 是加载现有YOLO权重所需的兼容代码，整理时保留原模块名，避免PyTorch权重反序列化失败。
- 当前任务状态主要保存在单个FastAPI进程内，适合作品演示和中小规模部署；多实例部署需要进一步引入持久化任务队列或独立视频处理服务。

## 开源说明

项目中的YOLO模型兼容代码基于 [PeterH0323/Smart_Construction](https://github.com/PeterH0323/Smart_Construction) 整理和扩展，保留 GPL-3.0 许可证。FastAPI服务、Vue前端、视频任务、ISAPI/RTSP接入、OSNet人物合并、数据库和短信告警属于本项目的工程扩展部分。

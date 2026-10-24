# 第三方组件

本仓库独立实现 Python 提示桥接、队列、控制面板和 OneBot HTTP 客户端。不打包 QQ、Node、NapCat 源码或二进制，不把它们的许可证改为 MIT。

可选便携安装器下载：

- 项目：[NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- 版本：4.18.41
- 文件：`NapCat.Shell.Windows.Node.zip`
- SHA256：`51fbb10e7bcd56b0c2a6de86c37811e7518330e48a137ac689ab1e4d8627d3f0`
- 来源：[官方 release](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.41)
- 许可证：[NapCat 上游 LICENSE](https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE)

上游当前为 Limited Redistribution License for NapCat，包含非商业用途和再分发条件；准确约束以上游原文为准。安装器保持上游包内文件原样。目标脚本、Python 解释器及其依赖也各有自己的许可证。

## 可选文件读取组件

`requirements.txt` 固定以下版本，由 pip 安装，不将其源码或二进制打进本项目源代码包。许可证名称取自对应安装包元数据，准确条件以组件原始许可证为准。

| 组件 | 版本 | 用途 | 元数据中的许可证 |
|---|---|---|---|
| [pypdf](https://pypi.org/project/pypdf/6.21.2/) | 6.21.2 | PDF 文字提取 | BSD-3-Clause |
| [openpyxl](https://pypi.org/project/openpyxl/3.1.5/) | 3.1.5 | XLSX/XLSM 单元格 | MIT |
| [xlrd](https://pypi.org/project/xlrd/2.0.2/) | 2.0.2 | 旧 XLS 单元格 | BSD |
| [striprtf](https://pypi.org/project/striprtf/0.0.32/) | 0.0.32 | RTF 文字提取 | BSD-3-Clause |
| [defusedxml](https://pypi.org/project/defusedxml/0.7.1/) | 0.7.1 | openpyxl 使用的 XML 解析保护 | PSFL |
| [et_xmlfile](https://pypi.org/project/et-xmlfile/2.0.0/) | 2.0.0 | openpyxl 依赖 | MIT |

测试中的 `tests/fixtures/example.xls` 为本项目生成的中性测试数据，不包含用户文件。生成时使用 xlwt 1.3.0，读取和运行测试均不依赖 xlwt。

当前资料核对日期：2026-10-24。后续组件升级应同时更新版本、校验值、兼容性测试和此说明。

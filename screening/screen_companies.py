#!/usr/bin/env python3
"""科创企业粗筛流水线（创新积分2.0近似口径，全程免费数据源，零付费API调用）。

数据源（全部免费、无Key）：
  1) 东方财富业绩报表 ak.stock_yjbb_em  —— 全A股营收/增速/ROE/所处行业（一次批量调用）
  2) 巨潮资讯公司画像 ak.stock_profile_cninfo —— 成立日期/注册地址/主营业务（逐企）
  3) 新浪财经利润表 ak.stock_financial_report_sina —— 最新年报研发费用/营收（逐企）
  4) 本地已缓存专利底稿（智慧芽历史抓取，仅读取缓存，不发起任何新API调用）

流程：行业映射→8大技术领域 → 剔除ST → 财务预排序取TopN → 逐企补研发费用与工商画像
     → 创新积分2.0引擎打分（上市样本同期群分位数标定 + 缺失值动态调权）
     → 写入 SQLite screened_companies 表 + data/screening/ JSON快照。

用法：.venv/bin/python screening/screen_companies.py [--top 150] [--date 20251231]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "data_layer"))

import warnings
warnings.filterwarnings("ignore")

import akshare as ak                      # noqa: E402
import innovation2                       # noqa: E402
import patent_quality                    # noqa: E402
import config                            # noqa: E402

OUT_DIR = config.DATA_DIR / "screening"
OUT_DIR.mkdir(parents=True, exist_ok=True)
(OUT_DIR / "companies").mkdir(exist_ok=True)

# ---------- 东财行业 → 创新积分2.0 八大技术领域 ----------
INDUSTRY_DOMAIN = {
    # 新一代信息技术
    "半导体": "新一代信息技术", "消费电子": "新一代信息技术", "光学光电子": "新一代信息技术",
    "电子元件": "新一代信息技术", "电子化学品": "新一代信息技术", "计算机设备": "新一代信息技术",
    "软件开发": "新一代信息技术", "IT服务": "新一代信息技术", "通信设备": "新一代信息技术",
    "通信服务": "新一代信息技术", "互联网服务": "新一代信息技术",
    # 生物医药
    "生物制品": "生物医药", "化学制药": "生物医药", "医疗器械": "生物医药",
    "中药": "生物医药", "医疗服务": "生物医药",
    # 航空航天
    "航天航空": "航空航天",
    # 新能源与节能
    "电池": "新能源与节能", "光伏设备": "新能源与节能", "风电设备": "新能源与节能",
    "电网设备": "新能源与节能", "电源设备": "新能源与节能", "电机": "新能源与节能",
    "能源金属": "新能源与节能",
    # 先进制造与自动化
    "自动化设备": "先进制造与自动化", "专用设备": "先进制造与自动化", "通用设备": "先进制造与自动化",
    "仪器仪表": "先进制造与自动化", "工程机械": "先进制造与自动化", "轨交设备": "先进制造与自动化",
    "船舶制造": "先进制造与自动化", "工业母机": "先进制造与自动化",
    # 新材料
    "化学制品": "新材料", "化学原料": "新材料", "化纤行业": "新材料",
    "非金属材料": "新材料", "小金属": "新材料", "塑料制品": "新材料",
    # 资源与环境
    "环保行业": "资源与环境", "环境治理": "资源与环境",
}

# 已缓存底稿企业：股票代码 → 统一社会信用代码（company_key）
CACHED_KEY_BY_CODE = {
    "300750": "91350900587527783P",   # 宁德时代
}
# 额外纳入的已缓存非上市主体（深度分析样例）
EXTRA_CACHED = [
    {"company_key": "91110302801786752A", "name": "北京北方华创微电子装备有限公司",
     "domain": "新一代信息技术", "city": "北京市", "province": "北京市"},
]

# ---------- 城市坐标（市政府驻地近似，WGS84/GCJ02差异对演示无影响） ----------
CITY_COORDS = {
    "北京市": (39.904, 116.407), "上海市": (31.230, 121.474), "天津市": (39.084, 117.201),
    "重庆市": (29.563, 106.551), "广州市": (23.129, 113.264), "深圳市": (22.543, 114.058),
    "杭州市": (30.274, 120.155), "南京市": (32.060, 118.797), "苏州市": (31.299, 120.585),
    "无锡市": (31.491, 120.312), "常州市": (31.811, 119.974), "南通市": (31.980, 120.894),
    "徐州市": (34.205, 117.284), "扬州市": (32.394, 119.412), "镇江市": (32.188, 119.425),
    "泰州市": (32.455, 119.923), "盐城市": (33.347, 120.163), "淮安市": (33.610, 119.015),
    "连云港市": (34.596, 119.221), "宿迁市": (33.963, 118.275),
    "合肥市": (31.821, 117.227), "芜湖市": (31.352, 118.433), "蚌埠市": (32.916, 117.389),
    "马鞍山市": (31.670, 118.507), "滁州市": (32.301, 118.317), "阜阳市": (32.890, 115.814),
    "安庆市": (30.543, 117.063), "宣城市": (30.940, 118.759), "铜陵市": (30.945, 117.812),
    "福州市": (26.074, 119.296), "厦门市": (24.479, 118.089), "泉州市": (24.874, 118.676),
    "宁德市": (26.666, 119.548), "漳州市": (24.513, 117.647), "莆田市": (25.454, 119.008),
    "龙岩市": (25.075, 117.017), "三明市": (26.263, 117.638), "南平市": (26.641, 118.178),
    "南昌市": (28.682, 115.858), "赣州市": (25.831, 114.935), "九江市": (29.705, 116.001),
    "景德镇市": (29.269, 117.178), "上饶市": (28.455, 117.943), "宜春市": (27.815, 114.416),
    "吉安市": (27.113, 114.993), "抚州市": (27.949, 116.358), "新余市": (27.818, 114.917),
    "济南市": (36.651, 117.120), "青岛市": (36.067, 120.383), "烟台市": (37.464, 121.448),
    "潍坊市": (36.707, 119.162), "淄博市": (36.813, 118.055), "威海市": (37.513, 122.120),
    "临沂市": (35.104, 118.356), "济宁市": (35.415, 116.587), "泰安市": (36.200, 117.087),
    "德州市": (37.436, 116.359), "聊城市": (36.456, 115.985), "滨州市": (37.382, 117.971),
    "东营市": (37.434, 118.675), "枣庄市": (34.810, 117.323), "日照市": (35.416, 119.527),
    "菏泽市": (35.233, 115.481),
    "郑州市": (34.746, 113.625), "洛阳市": (34.619, 112.454), "新乡市": (35.303, 113.927),
    "许昌市": (34.036, 113.852), "南阳市": (32.990, 112.528), "焦作市": (35.215, 113.242),
    "平顶山市": (33.766, 113.193), "开封市": (34.797, 114.307), "安阳市": (36.098, 114.393),
    "商丘市": (34.414, 115.656), "周口市": (33.626, 114.697), "驻马店市": (33.012, 114.022),
    "信阳市": (32.147, 114.091), "漯河市": (33.581, 114.016), "三门峡市": (34.773, 111.200),
    "武汉市": (30.593, 114.305), "宜昌市": (30.692, 111.286), "襄阳市": (32.009, 112.122),
    "荆州市": (30.335, 112.240), "黄石市": (30.200, 115.039), "十堰市": (32.629, 110.798),
    "孝感市": (30.924, 113.957), "荆门市": (31.035, 112.199), "鄂州市": (30.391, 114.895),
    "黄冈市": (30.454, 114.872), "咸宁市": (29.841, 114.322), "随州市": (31.690, 113.382),
    "长沙市": (28.228, 112.939), "株洲市": (27.828, 113.134), "湘潭市": (27.830, 112.944),
    "衡阳市": (26.893, 112.572), "岳阳市": (29.357, 113.129), "常德市": (29.031, 111.699),
    "郴州市": (25.770, 113.015), "邵阳市": (27.239, 111.468), "益阳市": (28.554, 112.355),
    "娄底市": (27.700, 111.994), "永州市": (26.420, 111.613), "怀化市": (27.569, 110.002),
    "成都市": (30.573, 104.067), "绵阳市": (31.468, 104.679), "德阳市": (31.127, 104.398),
    "宜宾市": (28.752, 104.643), "泸州市": (28.872, 105.442), "南充市": (30.837, 106.111),
    "乐山市": (29.552, 103.766), "自贡市": (29.339, 104.778), "攀枝花市": (26.582, 101.719),
    "遂宁市": (30.533, 105.593), "内江市": (29.580, 105.058), "眉山市": (30.076, 103.848),
    "广安市": (30.456, 106.633), "达州市": (31.209, 107.468), "雅安市": (30.010, 103.042),
    "西安市": (34.341, 108.940), "咸阳市": (34.329, 108.709), "宝鸡市": (34.361, 107.238),
    "渭南市": (34.499, 109.510), "汉中市": (33.067, 107.023), "榆林市": (38.285, 109.734),
    "延安市": (36.585, 109.490), "安康市": (32.685, 109.029), "商洛市": (33.870, 109.940),
    "沈阳市": (41.805, 123.431), "大连市": (38.914, 121.615), "鞍山市": (41.108, 122.995),
    "抚顺市": (41.881, 123.957), "营口市": (40.667, 122.235), "锦州市": (41.095, 121.127),
    "丹东市": (40.000, 124.355), "葫芦岛市": (40.711, 120.837), "盘锦市": (41.120, 122.071),
    "长春市": (43.817, 125.324), "吉林市": (43.838, 126.549), "四平市": (43.166, 124.350),
    "哈尔滨市": (45.803, 126.535), "大庆市": (46.589, 125.104), "齐齐哈尔市": (47.354, 123.918),
    "石家庄市": (38.042, 114.515), "唐山市": (39.630, 118.180), "保定市": (38.874, 115.464),
    "廊坊市": (39.538, 116.683), "邯郸市": (36.626, 114.539), "沧州市": (38.304, 116.839),
    "邢台市": (37.071, 114.505), "衡水市": (37.739, 115.670), "张家口市": (40.768, 114.886),
    "承德市": (40.952, 117.963), "秦皇岛市": (39.935, 119.600),
    "太原市": (37.870, 112.548), "大同市": (40.076, 113.300), "长治市": (36.195, 113.116),
    "临汾市": (36.088, 111.519), "运城市": (35.026, 111.007), "晋城市": (35.491, 112.851),
    "呼和浩特市": (40.842, 111.750), "包头市": (40.657, 109.840), "鄂尔多斯市": (39.608, 109.781),
    "南宁市": (22.817, 108.366), "柳州市": (24.326, 109.428), "桂林市": (25.274, 110.299),
    "玉林市": (22.654, 110.181), "梧州市": (23.477, 111.279),
    "海口市": (20.044, 110.199), "三亚市": (18.253, 109.512),
    "贵阳市": (26.647, 106.630), "遵义市": (27.725, 106.927), "六盘水市": (26.592, 104.831),
    "昆明市": (25.038, 102.718), "曲靖市": (25.490, 103.796), "玉溪市": (24.352, 102.546),
    "兰州市": (36.061, 103.834), "天水市": (34.581, 105.724), "白银市": (36.545, 104.139),
    "西宁市": (36.617, 101.778), "银川市": (38.487, 106.231), "石嘴山市": (38.984, 106.384),
    "乌鲁木齐市": (43.826, 87.617), "克拉玛依市": (45.579, 84.889), "昌吉市": (44.014, 87.304),
    "拉萨市": (29.650, 91.140),
    "珠海市": (22.271, 113.577), "佛山市": (23.022, 113.122), "东莞市": (23.021, 113.752),
    "中山市": (22.517, 113.393), "惠州市": (23.112, 114.416), "江门市": (22.579, 113.082),
    "肇庆市": (23.047, 112.466), "汕头市": (23.354, 116.682), "湛江市": (21.270, 110.359),
    "茂名市": (21.663, 110.925), "清远市": (23.682, 113.056), "韶关市": (24.810, 113.597),
    "梅州市": (24.288, 116.122), "揭阳市": (23.550, 116.373), "潮州市": (23.657, 116.622),
    "汕尾市": (22.787, 115.375), "阳江市": (21.857, 111.982), "河源市": (23.744, 114.700),
    "云浮市": (22.915, 112.044),
    "温州市": (27.994, 120.699), "宁波市": (29.868, 121.544), "嘉兴市": (30.746, 120.755),
    "绍兴市": (30.030, 120.580), "金华市": (29.079, 119.647), "台州市": (28.656, 121.421),
    "湖州市": (30.894, 120.088), "衢州市": (28.970, 118.859), "丽水市": (28.468, 119.923),
    "舟山市": (29.985, 122.207),
    "延吉市": (42.891, 129.509), "恩施市": (30.295, 109.480), "吉首市": (28.262, 109.698),
    "都匀市": (26.259, 107.517), "凯里市": (26.567, 107.981), "兴义市": (25.092, 104.896),
    "大理市": (25.606, 100.267), "个旧市": (23.359, 103.160), "景洪市": (22.000, 100.797),
    "库尔勒市": (41.726, 86.174), "伊宁市": (43.909, 81.278), "阿克苏市": (41.167, 80.261),
    "喀什市": (39.468, 75.990), "哈密市": (42.819, 93.515), "吐鲁番市": (42.951, 89.189),
    "石河子市": (44.306, 86.080), "阿拉尔市": (40.547, 81.280),
    "儋州市": (19.521, 109.581), "三沙市": (16.831, 112.333),
    "海东市": (36.502, 102.104), "格尔木市": (36.402, 94.903),
    "日喀则市": (29.267, 88.881), "林芝市": (29.649, 94.361),
    "吴忠市": (37.997, 106.199), "中卫市": (37.500, 105.196),
    "乌海市": (39.655, 106.794), "赤峰市": (42.258, 118.887), "通辽市": (43.653, 122.243),
    "呼伦贝尔市": (49.212, 119.766), "巴彦淖尔市": (40.743, 107.388), "乌兰察布市": (40.994, 113.133),
    "锡林浩特市": (43.933, 116.086), "乌兰浩特市": (46.072, 122.094), "满洲里市": (49.598, 117.379),
    "二连浩特市": (43.653, 111.977),
    "牡丹江市": (44.552, 129.633), "佳木斯市": (46.800, 130.319), "绥化市": (46.653, 126.969),
    "鸡西市": (45.295, 130.969), "双鸭山市": (46.646, 131.159), "鹤岗市": (47.350, 130.298),
    "伊春市": (47.727, 128.841), "七台河市": (45.771, 131.003), "黑河市": (50.245, 127.529),
    "大兴安岭地区": (52.335, 124.711),
    "通化市": (41.728, 125.939), "白山市": (41.940, 126.424), "松原市": (45.141, 124.825),
    "白城市": (45.620, 122.839), "辽源市": (42.888, 125.144), "梅河口市": (42.539, 125.711),
    "本溪市": (41.294, 123.766), "阜新市": (42.022, 121.670), "辽阳市": (41.268, 123.237),
    "铁岭市": (42.286, 123.844), "朝阳市": (41.573, 120.451), "兴城市": (40.610, 120.756),
    "瓦房店市": (39.627, 121.979), "庄河市": (39.681, 122.967),
    "阳泉市": (37.857, 113.580), "朔州市": (39.331, 112.433), "晋中市": (37.687, 112.753),
    "忻州市": (38.416, 112.734), "吕梁市": (37.519, 111.144), "侯马市": (35.619, 111.372),
    "济源市": (35.067, 112.602), "濮阳市": (35.762, 115.029), "鹤壁市": (35.748, 114.297),
    "义马市": (34.747, 111.874), "灵宝市": (34.517, 110.894), "邓州市": (32.688, 112.087),
    "永城市": (33.929, 116.449), "长葛市": (34.216, 113.768), "禹州市": (34.141, 113.488),
    "汝州市": (34.167, 112.844), "巩义市": (34.748, 113.022), "新郑市": (34.396, 113.740),
    "登封市": (34.454, 113.050), "荥阳市": (34.787, 113.383), "新密市": (34.539, 113.391),
    "偃师市": (34.727, 112.790), "孟州市": (34.907, 112.791), "沁阳市": (35.088, 112.951),
    "卫辉市": (35.398, 114.065), "辉县市": (35.462, 113.805), "林州市": (36.083, 113.819),
    "舞钢市": (33.302, 113.526), "项城市": (33.466, 114.875),
    "仙桃市": (30.362, 113.454), "潜江市": (30.402, 112.899), "天门市": (30.663, 113.166),
    "神农架林区": (31.744, 110.676), "丹江口市": (32.540, 111.513), "老河口市": (32.387, 111.683),
    "枣阳市": (32.128, 112.774), "宜城市": (31.720, 112.258), "钟祥市": (31.168, 112.588),
    "应城市": (30.928, 113.573), "安陆市": (31.256, 113.689), "汉川市": (30.661, 113.839),
    "石首市": (29.721, 112.425), "洪湖市": (29.827, 113.476), "松滋市": (30.170, 111.757),
    "麻城市": (31.172, 115.009), "武穴市": (29.844, 115.561), "赤壁市": (29.725, 113.900),
    "广水市": (31.617, 113.826), "恩施市": (30.295, 109.480), "利川市": (30.291, 108.936),
    "浏阳市": (28.163, 113.643), "宁乡市": (28.277, 112.552), "醴陵市": (27.646, 113.497),
    "湘乡市": (27.734, 112.535), "韶山市": (27.915, 112.526), "耒阳市": (26.422, 112.860),
    "常宁市": (26.421, 112.399), "武冈市": (26.727, 110.632), "汨罗市": (28.806, 113.067),
    "临湘市": (29.477, 113.450), "津市市": (29.606, 111.877), "沅江市": (28.844, 112.354),
    "资兴市": (25.976, 113.236), "洪江市": (27.209, 109.836), "冷水江市": (27.686, 111.435),
    "涟源市": (27.693, 111.664), "吉首市": (28.262, 109.698),
    "都江堰市": (30.988, 103.647), "彭州市": (30.990, 103.958), "邛崃市": (30.410, 103.464),
    "崇州市": (30.630, 103.673), "简阳市": (30.411, 104.547), "广汉市": (30.977, 104.282),
    "什邡市": (31.127, 104.167), "绵竹市": (31.338, 104.224), "江油市": (31.778, 104.746),
    "阆中市": (31.558, 106.005), "华蓥市": (30.390, 106.783), "万源市": (32.082, 108.034),
    "西昌市": (27.894, 102.264), "康定市": (30.055, 101.964), "马尔康市": (31.906, 102.206),
    "峨眉山市": (29.601, 103.484), "隆昌市": (29.339, 105.287),
    "仁怀市": (27.792, 106.401), "赤水市": (28.590, 105.697), "清镇市": (26.556, 106.471),
    "福泉市": (26.686, 107.520), "盘州市": (25.710, 104.471), "兴仁市": (25.435, 105.186),
    "安宁市": (24.919, 102.478), "宣威市": (26.219, 104.104), "腾冲市": (25.020, 98.491),
    "楚雄市": (25.033, 101.546), "蒙自市": (23.396, 103.364), "弥勒市": (24.411, 103.414),
    "文山市": (23.386, 104.232), "香格里拉市": (27.829, 99.700), "泸水市": (25.822, 98.857),
    "瑞丽市": (24.013, 97.852), "芒市": (24.434, 98.588),
    "合作市": (35.000, 102.911), "临夏市": (35.604, 103.210), "玉门市": (40.292, 97.045),
    "敦煌市": (40.142, 94.662), "金昌市": (38.520, 102.188), "嘉峪关市": (39.772, 98.290),
    "武威市": (37.928, 102.638), "张掖市": (38.926, 100.450), "平凉市": (35.543, 106.665),
    "酒泉市": (39.732, 98.494), "庆阳市": (35.709, 107.643), "定西市": (35.580, 104.626),
    "陇南市": (33.401, 104.922),
    "德令哈市": (37.369, 97.361), "玉树市": (33.004, 97.009), "茫崖市": (38.247, 90.856),
    "灵武市": (38.103, 106.340), "青铜峡市": (38.025, 106.079),
    "阜康市": (44.157, 87.987), "博乐市": (44.854, 82.051), "阿拉山口市": (45.170, 82.567),
    "霍尔果斯市": (44.201, 80.420), "奎屯市": (44.426, 84.903), "乌苏市": (44.418, 84.713),
    "沙湾市": (44.326, 85.619), "塔城市": (46.748, 82.987), "阿勒泰市": (47.827, 88.132),
    "和田市": (37.112, 79.913), "阿图什市": (39.716, 76.168), "库车市": (41.717, 82.962),
    "图木舒克市": (39.867, 79.074), "五家渠市": (44.166, 87.543), "北屯市": (47.353, 87.824),
    "铁门关市": (41.827, 85.501), "双河市": (44.840, 82.353), "可克达拉市": (43.947, 81.044),
    "昆玉市": (37.209, 79.291), "胡杨河市": (44.692, 84.827), "新星市": (42.797, 93.744),
    "白杨市": (46.727, 82.897),
}
PROVINCE_CAPITAL = {
    "河北省": "石家庄市", "山西省": "太原市", "辽宁省": "沈阳市", "吉林省": "长春市",
    "黑龙江省": "哈尔滨市", "江苏省": "南京市", "浙江省": "杭州市", "安徽省": "合肥市",
    "福建省": "福州市", "江西省": "南昌市", "山东省": "济南市", "河南省": "郑州市",
    "湖北省": "武汉市", "湖南省": "长沙市", "广东省": "广州市", "海南省": "海口市",
    "四川省": "成都市", "贵州省": "贵阳市", "云南省": "昆明市", "陕西省": "西安市",
    "甘肃省": "兰州市", "青海省": "西宁市", "广西壮族自治区": "南宁市",
    "内蒙古自治区": "呼和浩特市", "西藏自治区": "拉萨市", "宁夏回族自治区": "银川市",
    "新疆维吾尔自治区": "乌鲁木齐市",
}


def parse_region(addr: str):
    """从注册地址解析 (省, 市)。支持自贸区/片区/离岸地址兜底。"""
    if not addr:
        return None, None
    prov = None
    m = re.match(r"(北京市|上海市|天津市|重庆市)", addr)
    if m:
        return m.group(1), m.group(1)
    # 中国（X）自由贸易试验区……
    m = re.match(r"中国（([^）]+)）自由贸易试验区(.*)", addr)
    if m:
        zone, rest = m.group(1), m.group(2)
        prov = {"上海": "上海市", "天津": "天津市", "重庆": "重庆市", "北京": "北京市",
                "山东": "山东省", "广东": "广东省", "福建": "福建省", "浙江": "浙江省",
                "江苏": "江苏省", "四川": "四川省", "湖北": "湖北省", "湖南": "湖南省",
                "河南": "河南省", "河北": "河北省", "陕西": "陕西省", "辽宁": "辽宁省",
                "吉林": "吉林省", "黑龙江": "黑龙江省", "安徽": "安徽省", "江西": "江西省",
                "广西": "广西壮族自治区", "云南": "云南省", "贵州": "贵州省", "甘肃": "甘肃省",
                "海南": "海南省", "山西": "山西省", "内蒙古": "内蒙古自治区",
                "新疆": "新疆维吾尔自治区", "宁夏": "宁夏回族自治区", "西藏": "西藏自治区"}.get(zone)
        if zone in ("上海", "天津", "重庆", "北京"):
            return zone + "市", zone + "市"
        m2 = re.search(r"([\u4e00-\u9fa5]{2,6})片区", rest)
        if m2:
            cand = m2.group(1) + "市"
            if cand in CITY_COORDS:
                return prov, cand
        addr = rest  # 继续往下扫城市名
    m = re.match(r"([^省]+省|内蒙古自治区|广西壮族自治区|西藏自治区|宁夏回族自治区|新疆维吾尔自治区)", addr)
    if m:
        prov = m.group(1)
    m = re.search(r"([^省市区县州]{2,12}?(?:市|自治州|地区|盟))", addr)
    city = m.group(1) if m else None
    if city and city.endswith("自治州"):
        city = city.replace("自治州", "市")
    # 兜底：地址里直接出现已知城市名（如"烟台开发区"）
    if not city or city not in CITY_COORDS:
        for cname in sorted(CITY_COORDS, key=len, reverse=True):
            stem = cname[:-1]
            if len(stem) >= 2 and stem in addr:
                city = cname
                break
    if not prov:
        for p in PROVINCE_CAPITAL:
            if addr.startswith(p.replace("省", "").replace("市", "")[:2]):
                prov = p
                break
    return prov, city


def city_coords(prov, city):
    if city and city in CITY_COORDS:
        return CITY_COORDS[city], city
    if prov and prov in PROVINCE_CAPITAL:
        cap = PROVINCE_CAPITAL[prov]
        return CITY_COORDS[cap], (city or cap)
    if prov and prov in CITY_COORDS:  # 直辖市
        return CITY_COORDS[prov], prov
    return CITY_COORDS["北京市"], (city or "未知")


def fetch_yjbb(date: str):
    try:
        df = ak.stock_yjbb_em(date=date)
        if df is not None and len(df) > 1000:
            return df, date
    except Exception as exc:
        print(f"[yjbb] {date} 失败: {repr(exc)[:120]}", flush=True)
    fb = "20250930" if date != "20250930" else "20250630"
    df = ak.stock_yjbb_em(date=fb)
    return df, fb


def latest_annual_income(code: str):
    prefix = "sh" if code.startswith("6") else ("bj" if code.startswith(("4", "8")) else "sz")
    inc = ak.stock_financial_report_sina(stock=prefix + code, symbol="利润表")
    norm = inc["报告日"].astype(str).str.replace("-", "", regex=False)
    d = inc[norm.str.endswith("1231")].copy()
    if d.empty:
        return None
    d = d.sort_values("报告日", ascending=False)
    row = d.iloc[0]

    def num(*keys):
        for k in keys:
            if k in row.index and row[k] not in (None, "", "--"):
                try:
                    v = float(row[k])
                    if not math.isnan(v):
                        return v
                except (TypeError, ValueError):
                    pass
        return None

    return {"period": str(row["报告日"]),
            "revenue": num("营业收入", "营业总收入"),
            "rd_expense": num("研发费用"),
            "net_profit": num("净利润")}


def profile(code: str):
    p = ak.stock_profile_cninfo(symbol=code)
    r = p.iloc[0].to_dict()
    return {k: ("" if v is None else str(v)) for k, v in r.items()}


def retry(fn, *args, tries=2, wait=1.0, **kw):
    for i in range(tries):
        try:
            return fn(*args, **kw)
        except Exception as exc:
            if i == tries - 1:
                raise
            time.sleep(wait)


def percentile_benchmarks(values_by_domain: dict):
    """用本次上市样本按领域标定 p25/p50/p75/p90（粗筛口径）。"""
    import numpy as np
    bench = {k: dict(v) for k, v in innovation2.BASE_BENCH.items()}
    for dom, per_ind in values_by_domain.items():
        for ind, vals in per_ind.items():
            vals = [v for v in vals if v is not None]
            if len(vals) < 8:
                continue
            q = np.percentile(vals, [25, 50, 75, 90])
            b = {"p25": float(q[0]), "p50": float(q[1]), "p75": float(q[2]), "p90": float(q[3])}
            if b["p90"] <= b["p25"]:
                continue
            for st in innovation2.STAGES:
                bench[(dom, st, ind)] = b
    return bench


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=150, help="精筛企业数（逐企拉年报+画像）")
    ap.add_argument("--date", default="20251231", help="业绩报表报告期")
    ap.add_argument("--keep", type=int, default=60, help="最终名单保留数")
    ap.add_argument("--from-cache", action="store_true", help="从 keep_cache.json 直接落库")
    a = ap.parse_args()

    t0 = time.time()
    if a.from_cache:
        cache = json.loads((OUT_DIR / "keep_cache.json").read_text(encoding="utf-8"))
        write_db(cache["keep"], cache["used_date"], t0, -1, -1)
        return
    yjbb, used_date = fetch_yjbb(a.date)
    print(f"[yjbb] 报告期 {used_date}，全A {len(yjbb)} 行", flush=True)
    yjbb.to_json(OUT_DIR / f"yjbb_{used_date}.json", orient="records", force_ascii=False)

    # ---- 行业过滤 ----
    rows = []
    unmapped = {}
    for _, r in yjbb.iterrows():
        name = str(r.get("股票简称", ""))
        ind = str(r.get("所处行业", "") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        dom = INDUSTRY_DOMAIN.get(ind)
        if not dom:
            unmapped[ind] = unmapped.get(ind, 0) + 1
            continue
        code = str(r.get("股票代码", "")).zfill(6)

        def f(k):
            v = r.get(k)
            try:
                v = float(v)
                return None if math.isnan(v) else v
            except (TypeError, ValueError):
                return None

        rev = f("营业总收入-营业总收入")
        growth = f("营业总收入-同比增长")
        roe = f("净资产收益率")
        if rev is None or rev <= 0:
            continue
        rows.append({"code": code, "name": name, "industry": ind, "domain": dom,
                     "revenue_yuan": rev, "rev_growth_pct": growth, "roe_pct": roe})
    print(f"[universe] 硬科技行业 {len(rows)} 家；未映射行业Top: "
          f"{sorted(unmapped.items(), key=lambda x:-x[1])[:12]}", flush=True)

    # ---- 预排序（增速40% + ROE35% + 规模25%，分位数） ----
    import numpy as np

    def pct_ranks(vals):
        arr = np.array([v if v is not None else np.nan for v in vals])
        ok = ~np.isnan(arr)
        out = np.zeros(len(arr))
        if ok.sum() > 1:
            order = arr[ok].argsort().argsort() / (ok.sum() - 1)
            out[ok] = order
        return out

    g = pct_ranks([r["rev_growth_pct"] for r in rows])
    ro = pct_ranks([r["roe_pct"] for r in rows])
    sc = pct_ranks([math.log(max(r["revenue_yuan"], 1)) for r in rows])
    for i, r in enumerate(rows):
        r["prescore"] = round(float(0.40 * g[i] + 0.35 * ro[i] + 0.25 * sc[i]), 4)
    rows.sort(key=lambda r: -r["prescore"])
    short = rows[:a.top]
    for must in ("300750", "002371"):
        if not any(r["code"] == must for r in short):
            hit = next((r for r in rows if r["code"] == must), None)
            if hit:
                short.append(hit)
    print(f"[shortlist] 精筛 {len(short)} 家，开始逐企采集（免费接口）...", flush=True)

    # ---- 逐企采集 ----
    enriched = []
    for i, r in enumerate(short):
        code = r["code"]
        rec = dict(r)
        try:
            prof = retry(profile, code)
            rec["full_name"] = prof.get("公司名称") or r["name"]
            rec["founded"] = prof.get("成立日期", "")
            rec["reg_addr"] = prof.get("注册地址", "")
            rec["office_addr"] = prof.get("办公地址", "")
            rec["main_business"] = prof.get("主营业务", "")[:300]
            rec["website"] = prof.get("官方网站", "")
        except Exception as exc:
            rec["profile_error"] = repr(exc)[:150]
            rec["full_name"] = r["name"]
        try:
            inc = retry(latest_annual_income, code)
            rec["annual_income"] = inc
        except Exception as exc:
            rec["income_error"] = repr(exc)[:150]
            rec["annual_income"] = None
        enriched.append(rec)
        if (i + 1) % 10 == 0:
            print(f"  进度 {i+1}/{len(short)}  耗时{time.time()-t0:.0f}s", flush=True)
        time.sleep(0.25)

    # ---- 指标口径 ----
    for rec in enriched:
        inc = rec.get("annual_income") or {}
        rd, rev_annual = inc.get("rd_expense"), inc.get("revenue")
        rec["inputs"] = {
            innovation2.RD_EXPENSE: round(rd / 1e4, 2) if rd else None,
            innovation2.RD_RATIO: round(rd / rev_annual * 100, 2) if rd and rev_annual else None,
            innovation2.MAIN_REVENUE: round((rev_annual or rec["revenue_yuan"]) / 1e4, 2),
            innovation2.REV_GROWTH: rec.get("rev_growth_pct"),
            innovation2.ROE: rec.get("roe_pct"),
        }
        founded = rec.get("founded", "")
        years = None
        if founded:
            try:
                years = (datetime.now() - datetime.strptime(founded[:10], "%Y-%m-%d")).days / 365.25
            except ValueError:
                pass
        rec["stage"] = innovation2.stage_from_years(years)
        rec["founded_years"] = round(years, 1) if years else None
        prov, city = parse_region(rec.get("reg_addr", ""))
        if not city or city not in CITY_COORDS:
            prov2, city2 = parse_region(rec.get("office_addr", ""))
            if city2 and city2 in CITY_COORDS:
                prov, city = prov2 or prov, city2
        (lat, lng), city_used = city_coords(prov, city)
        rec["province"], rec["city"] = prov or "", city_used
        rec["lat"], rec["lng"] = lat, lng

    # 已缓存专利企业：回填发明专利授权量（只读本地缓存，零API调用）
    for rec in enriched:
        key = CACHED_KEY_BY_CODE.get(rec["code"])
        if key:
            rec["company_key"] = key
            try:
                pats = patent_quality.parse_patents(config.RAW_DIR, key)
                # 与 run_engine.derive_patent_inputs 同口径（发明+发明实审中）
                n_inv = sum(1 for p in pats if p["type"] in ("invention", "invention_pending"))
                rec["inputs"][innovation2.INVENTION_GRANTS] = float(n_inv)
                rec["cached_patents"] = len(pats)
            except Exception as exc:
                rec["patent_cache_error"] = repr(exc)[:150]
        else:
            rec["company_key"] = f"A{rec['code']}"

    # ---- 同期群标定 + 打分 ----
    vals = {}
    for rec in enriched:
        d = vals.setdefault(rec["domain"], {})
        for ind, v in rec["inputs"].items():
            if v is not None:
                d.setdefault(ind, []).append(v)
    bench = percentile_benchmarks(vals)
    (OUT_DIR / "cohort_benchmarks.json").write_text(
        json.dumps({f"{k[0]}|{k[1]}|{k[2]}" if isinstance(k, tuple) else k: v
                    for k, v in bench.items()}, ensure_ascii=False, indent=1), encoding="utf-8")

    for rec in enriched:
        res = innovation2.compute_innovation(rec["inputs"], domain=rec["domain"],
                                             stage=rec["stage"], bonus={}, bench=bench)
        rec["innovation"] = res

    enriched.sort(key=lambda r: -r["innovation"]["core_score"])
    keep = enriched[:a.keep]
    # 强制保留已缓存底稿的旗舰样例（宁德时代/北方华创上市公司主体）
    for must in ("300750", "002371"):
        if not any(r.get("code") == must for r in keep):
            hit = next((r for r in enriched if r.get("code") == must), None)
            if hit:
                keep.append(hit)

    # 追加已缓存非上市样例主体
    for extra in EXTRA_CACHED:
        key = extra["company_key"]
        try:
            pats = patent_quality.parse_patents(config.RAW_DIR, key)
        except Exception:
            pats = []
        n_inv = sum(1 for p in pats if p["type"] in ("invention", "invention_pending"))
        inputs = {innovation2.INVENTION_GRANTS: float(n_inv)}
        res = innovation2.compute_innovation(inputs, domain=extra["domain"], stage="成长期",
                                             bonus={}, bench=bench)
        (lat, lng), _ = city_coords(extra["province"], extra["city"])
        keep.append({"company_key": key, "name": extra["name"], "full_name": extra["name"],
                     "code": "", "domain": extra["domain"], "stage": "成长期",
                     "city": extra["city"], "province": extra["province"],
                     "lat": lat, "lng": lng, "inputs": inputs, "innovation": res,
                     "cached_patents": len(pats), "is_cached_entity": True,
                     "main_business": "半导体刻蚀/薄膜沉积等集成电路装备（已缓存专利底稿，深度分析样例）"})

    # ---- 落库 ----
    (OUT_DIR / "keep_cache.json").write_text(
        json.dumps({"used_date": used_date, "keep": keep}, ensure_ascii=False, default=str),
        encoding="utf-8")
    write_db(keep, used_date, t0, len(rows), len(enriched))


def write_db(keep, used_date, t0, n_universe, n_enriched):
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS screened_companies(
      company_key TEXT PRIMARY KEY, name TEXT, full_name TEXT, code TEXT,
      domain TEXT, stage TEXT, industry TEXT, province TEXT, city TEXT,
      lat REAL, lng REAL, core_score REAL, bonus_score REAL, total_score REAL,
      grade TEXT, prescore REAL, cached_patents INTEGER DEFAULT 0,
      is_cached_entity INTEGER DEFAULT 0, missing_json TEXT, inputs_json TEXT,
      metrics_json TEXT, profile_json TEXT, report_period TEXT,
      source_note TEXT, updated_at TEXT)""")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 同城散点微偏移（仅显示用，确定性）
    seen = {}
    for rec in keep:
        c = rec["city"]
        k = seen.get(c, 0)
        seen[c] = k + 1
        if k:
            ang = k * 2.399963
            rad = 0.06 + 0.05 * (k % 5)
            rec["lat"] = round(rec["lat"] + rad * math.sin(ang), 4)
            rec["lng"] = round(rec["lng"] + rad * math.cos(ang), 4)
        inn = rec["innovation"]
        profile_json = json.dumps({k2: rec.get(k2, "") for k2 in
                                   ("founded", "founded_years", "reg_addr", "main_business",
                                    "website", "annual_income", "revenue_yuan",
                                    "rev_growth_pct", "roe_pct", "industry")},
                                  ensure_ascii=False)
        conn.execute("INSERT OR REPLACE INTO screened_companies(company_key,name,full_name,code,"
                     "domain,stage,industry,province,city,lat,lng,core_score,bonus_score,"
                     "total_score,grade,prescore,cached_patents,is_cached_entity,missing_json,"
                     "inputs_json,metrics_json,profile_json,report_period,source_note,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rec["company_key"], rec.get("name") or rec.get("full_name"),
                      rec.get("full_name"), rec.get("code", ""), rec["domain"], rec["stage"],
                      rec.get("industry", ""), rec.get("province", ""), rec["city"],
                      rec["lat"], rec["lng"], inn["core_score"], inn["bonus_score"],
                      inn["total_score"], inn["grade"], rec.get("prescore"),
                      rec.get("cached_patents", 0), 1 if rec.get("is_cached_entity") else 0,
                      json.dumps(inn["missing_reweighted"], ensure_ascii=False),
                      json.dumps(rec["inputs"], ensure_ascii=False),
                      json.dumps(inn["metrics"], ensure_ascii=False), profile_json,
                      used_date,
                      "免费口径：东财业绩报表+巨潮公司画像+新浪年报(研发费用)；专利仅读本地缓存；加分项未采集",
                      now))
        (OUT_DIR / "companies" / f"{rec['company_key']}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    conn.commit()

    grades = {}
    doms = {}
    for rec in keep:
        grades[rec["innovation"]["grade"]] = grades.get(rec["innovation"]["grade"], 0) + 1
        doms[rec["domain"]] = doms.get(rec["domain"], 0) + 1
    summary = {"as_of": used_date, "universe": n_universe, "enriched": n_enriched,
               "kept": len(keep), "grades": grades, "domains": doms,
               "elapsed_sec": round(time.time() - t0, 1),
               "top10": [{"name": r.get("name") or r.get("full_name"),
                          "city": r["city"], "domain": r["domain"],
                          "core": r["innovation"]["core_score"],
                          "grade": r["innovation"]["grade"]} for r in keep[:10]]}
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)


if __name__ == "__main__":
    main()
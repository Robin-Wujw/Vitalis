"""Verified Zepp OS workout modes and Chinese presentation labels.

Codes follow the public Zepp OS activity enum used by Gadgetbridge. Unknown future
codes remain explicit instead of being guessed into a known activity.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SportMode:
    code: str
    label_zh: str
    category: str
    family: str
    recognition_confidence: str = "HIGH"
    recognition_confidence_label: str = "较高"
    recognition_source: str = "public_zepp_enum"
    recognition_source_label: str = "公开 Zepp/Huami 运动枚举"


def _mode(
    code: str,
    label: str,
    category: str = "other",
    family: str = "skill",
    **recognition: str,
) -> SportMode:
    return SportMode(
        code=code,
        label_zh=label,
        category=category,
        family=family,
        **recognition,
    )


ZEPP_SPORT_MODES: dict[int, SportMode] = {
    0x01: _mode("outdoor_running", "户外跑", "running", "aerobic"),
    0x02: _mode("treadmill", "跑步机", "running", "aerobic"),
    0x03: _mode("walking", "健走", "walking", "aerobic"),
    0x04: _mode("outdoor_cycling", "户外骑行", "cycling", "aerobic"),
    0x05: _mode("free_training", "自由训练", "other", "mixed"),
    0x06: _mode("pool_swimming", "泳池游泳", "swimming", "aerobic"),
    0x07: _mode("open_water_swimming", "公开水域游泳", "swimming", "aerobic"),
    0x08: _mode("indoor_cycling", "室内骑行", "cycling", "aerobic"),
    0x09: _mode("elliptical", "椭圆机", "other", "aerobic"),
    0x0A: _mode("climbing", "攀登", "other", "mixed"),
    0x0F: _mode("hiking", "徒步", "walking", "aerobic"),
    0x11: _mode("tennis", "网球"),
    0x12: _mode("soccer_legacy", "足球", "other", "mixed"),
    0x15: _mode("jump_rope", "跳绳", "other", "aerobic"),
    0x17: _mode("rowing", "划船机", "other", "aerobic"),
    0x18: _mode("indoor_fitness", "室内健身", "other", "mixed"),
    0x29: _mode("curling", "冰壶"),
    0x2C: _mode("ice_skating", "滑冰", "other", "aerobic"),
    0x2D: _mode("indoor_ice_skating", "室内滑冰", "other", "aerobic"),
    0x30: _mode("bmx", "小轮车", "cycling", "mixed"),
    0x31: _mode("hiit", "高强度间歇训练", "hiit", "mixed"),
    0x32: _mode("core_training", "核心训练", "strength", "strength"),
    0x33: _mode("aerobic_combo", "有氧组合", "other", "aerobic"),
    0x34: _mode("strength_training", "力量训练", "strength", "strength"),
    0x35: _mode("stretching", "拉伸", "other", "mobility"),
    0x36: _mode("stair_climber", "爬楼机", "other", "aerobic"),
    0x37: _mode("flexibility", "柔韧性训练", "other", "mobility"),
    0x39: _mode("stepper", "踏步机", "other", "aerobic"),
    0x3B: _mode("gymnastics", "体操", "other", "mixed"),
    0x3C: _mode("yoga", "瑜伽", "yoga", "mobility"),
    0x3D: _mode("pilates", "普拉提", "yoga", "mobility"),
    0x40: _mode("fishing", "钓鱼"),
    0x41: _mode("sailing", "帆船"),
    0x42: _mode("water_rowing", "水上划船", "other", "aerobic"),
    0x43: _mode("skateboarding", "滑板", "other", "mixed"),
    0x45: _mode("roller_skating", "轮滑", "other", "aerobic"),
    0x46: _mode("rock_climbing", "攀岩", "other", "mixed"),
    0x47: _mode("ballet", "芭蕾", "other", "mobility"),
    0x48: _mode("belly_dance", "肚皮舞", "other", "aerobic"),
    0x49: _mode("square_dance", "广场舞", "other", "aerobic"),
    0x4A: _mode("street_dance", "街舞", "other", "aerobic"),
    0x4B: _mode("ballroom_dance", "交谊舞", "other", "aerobic"),
    0x4C: _mode("dance", "舞蹈", "other", "aerobic"),
    0x4D: _mode("zumba", "尊巴", "other", "aerobic"),
    0x4E: _mode("cricket", "板球"),
    0x4F: _mode("baseball", "棒球"),
    0x50: _mode("bowling", "保龄球"),
    0x51: _mode("squash", "壁球", "other", "mixed"),
    0x55: _mode("basketball", "篮球", "other", "mixed"),
    0x56: _mode("softball", "垒球"),
    0x57: _mode("gateball", "门球"),
    0x58: _mode("volleyball", "排球", "other", "mixed"),
    0x59: _mode("table_tennis", "乒乓球"),
    0x5B: _mode("handball", "手球", "other", "mixed"),
    0x5C: _mode("badminton", "羽毛球", "other", "mixed"),
    0x5D: _mode("archery", "射箭"),
    0x5E: _mode("equestrian", "马术"),
    0x5F: _mode("kendo", "剑道", "other", "mixed"),
    0x60: _mode("karate", "空手道", "other", "mixed"),
    0x61: _mode("boxing", "拳击", "other", "mixed"),
    0x62: _mode("judo", "柔道", "other", "mixed"),
    0x63: _mode("wrestling", "摔跤", "other", "mixed"),
    0x64: _mode("tai_chi", "太极", "other", "mobility"),
    0x65: _mode("muay_thai", "泰拳", "other", "mixed"),
    0x66: _mode("taekwondo", "跆拳道", "other", "mixed"),
    0x67: _mode("martial_arts", "武术", "other", "mixed"),
    0x68: _mode("kickboxing", "自由搏击", "other", "mixed"),
    0x6D: _mode("aerobics", "健美操", "other", "aerobic"),
    0x6F: _mode("mass_gymnastics", "团体操", "other", "aerobic"),
    0x70: _mode("latin_dance", "拉丁舞", "other", "aerobic"),
    0x71: _mode("jazz_dance", "爵士舞", "other", "aerobic"),
    0x72: _mode("cardio_combat", "有氧搏击", "other", "mixed"),
    0x73: _mode("hula_hoop", "呼啦圈", "other", "aerobic"),
    0x74: _mode("frisbee", "飞盘", "other", "mixed"),
    0x75: _mode("darts", "飞镖"),
    0x76: _mode("kite_flying", "放风筝"),
    0x77: _mode("tug_of_war", "拔河", "other", "strength"),
    0x7A: _mode("beach_volleyball", "沙滩排球", "other", "mixed"),
    0x81: _mode("parkour", "跑酷", "other", "mixed"),
    0x82: _mode("cross_training", "交叉训练", "strength", "mixed"),
    0x83: _mode("race_walking", "竞走", "walking", "aerobic"),
    0x84: _mode("driving", "驾驶"),
    0x8A: _mode("dragon_boat", "龙舟", "other", "mixed"),
    0x8C: _mode("kayaking", "皮划艇", "other", "aerobic"),
    0x8F: _mode("spinning", "动感单车", "cycling", "aerobic"),
    0x90: _mode("air_walker", "漫步机", "other", "aerobic"),
    0x91: _mode("wall_ball", "墙球", "other", "mixed"),
    0x92: _mode("folk_dance", "民族舞", "other", "aerobic"),
    0x93: _mode("jujitsu", "柔术", "other", "mixed"),
    0x94: _mode("fencing", "击剑", "other", "mixed"),
    0x95: _mode("horizontal_bar", "单杠", "strength", "strength"),
    0x96: _mode("parallel_bars", "双杠", "strength", "strength"),
    0x97: _mode("billiards", "台球"),
    0x98: _mode("sepak_takraw", "藤球", "other", "mixed"),
    0x99: _mode("dodgeball", "躲避球", "other", "mixed"),
    0x9A: _mode("water_polo", "水球", "swimming", "mixed"),
    0x9B: _mode("finswimming", "蹼泳", "swimming", "aerobic"),
    0x9C: _mode("artistic_swimming", "花样游泳", "swimming", "mixed"),
    0x9D: _mode("snorkeling", "浮潜", "swimming", "aerobic"),
    0x9E: _mode("ice_hockey", "冰球", "other", "mixed"),
    0x9F: _mode("swing", "秋千"),
    0xA0: _mode("shuffleboard", "沙狐球"),
    0xA1: _mode("table_football", "桌上足球"),
    0xA2: _mode("shuttlecock", "踢毽子", "other", "aerobic"),
    0xA3: _mode("somatosensory_game", "体感游戏", "other", "mixed"),
    0xA4: _mode("futsal", "室内足球", "other", "mixed"),
    0xA5: _mode("hip_hop", "嘻哈舞", "other", "aerobic"),
    0xA6: _mode("pole_dance", "钢管舞", "other", "mixed"),
    0xA7: _mode("battle_rope", "战绳", "strength", "mixed"),
    0xA8: _mode("breaking", "霹雳舞", "other", "mixed"),
    0xA9: _mode("hacky_sack", "沙包球", "other", "mixed"),
    0xAA: _mode("bocce", "地掷球"),
    0xAB: _mode("jai_alai", "回力球", "other", "mixed"),
    0xAC: _mode("flowriding", "模拟冲浪", "other", "mixed"),
    0xAD: _mode("chess", "国际象棋"),
    0xAE: _mode("checkers", "国际跳棋"),
    0xAF: _mode("weiqi", "围棋"),
    0xB0: _mode("bridge", "桥牌"),
    0xB1: _mode("board_game", "桌游"),
    0xB9: _mode("modern_dance", "现代舞", "other", "aerobic"),
    0xBD: _mode("esports", "电子竞技"),
    0xBF: _mode("soccer", "足球", "other", "mixed"),
}


CATEGORY_LABELS = {
    "strength": "力量训练",
    "running": "跑步",
    "cycling": "骑行",
    "swimming": "游泳",
    "walking": "步行与徒步",
    "hiit": "高强度间歇训练",
    "yoga": "瑜伽与灵活性训练",
    "other": "其他运动",
}

FAMILY_LABELS = {
    "aerobic": "有氧",
    "strength": "力量",
    "mobility": "灵活性与恢复",
    "mixed": "混合体能",
    "skill": "技巧与休闲",
}


def resolve_sport_mode(vendor_type_id: int | None, fallback: str = "") -> SportMode:
    if vendor_type_id is not None:
        known = ZEPP_SPORT_MODES.get(vendor_type_id)
        if known:
            return known
        return _mode(
            f"unknown_{vendor_type_id}",
            f"未知运动（编号 {vendor_type_id}）",
            "other",
            "skill",
            recognition_confidence="NONE",
            recognition_confidence_label="无法识别",
            recognition_source="unknown_vendor_code",
            recognition_source_label="厂商编号未公开",
        )

    fallback_modes = {
        "run": ZEPP_SPORT_MODES[0x01],
        "running": ZEPP_SPORT_MODES[0x01],
        "walking": ZEPP_SPORT_MODES[0x03],
        "ride": ZEPP_SPORT_MODES[0x04],
        "cycling": ZEPP_SPORT_MODES[0x04],
        "swimming": ZEPP_SPORT_MODES[0x06],
        "strength": ZEPP_SPORT_MODES[0x34],
        "yoga": ZEPP_SPORT_MODES[0x3C],
    }
    known_fallback = fallback_modes.get(fallback.lower())
    if known_fallback:
        return SportMode(
            code=known_fallback.code,
            label_zh=known_fallback.label_zh,
            category=known_fallback.category,
            family=known_fallback.family,
            recognition_confidence="MODERATE",
            recognition_confidence_label="中等",
            recognition_source="vendor_text_fallback",
            recognition_source_label="厂商文字类型",
        )
    return SportMode(
        code="unknown",
        label_zh="未知运动",
        category="other",
        family="skill",
        recognition_confidence="NONE",
        recognition_confidence_label="无法识别",
        recognition_source="missing_vendor_type",
        recognition_source_label="缺少厂商运动类型",
    )

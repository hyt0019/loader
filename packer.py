import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np
from decimal import Decimal, InvalidOperation
from itertools import permutations
import json
import os
import hashlib
import matplotlib

matplotlib.use('TkAgg')  # 使用 Tkinter 作为交互式后端


class ContainerPacker:
    def __init__(self, container, boxes, weight_threshold=100):
        self.container = tuple(Decimal(str(x)) for x in container)
        self.boxes = [(Decimal(str(l)), Decimal(str(w)), Decimal(str(h)), int(n), Decimal(str(weight)), t, i) for
                      i, (l, w, h, n, weight, t) in enumerate(boxes)]
        self.weight_threshold = Decimal(str(weight_threshold))
        self.container_volume = self.container[0] * self.container[1] * self.container[2]
        self.occupied_space = []
        self.packing_plan = []
        self.box_colors = []
        self.highlighted_boxes = set()

    def can_place(self, x, y, z, l, w, h, weight):
        """判断是否可以在指定位置放置箱子，并加入严格的物理支撑检测"""
        # 边界检测
        if (x + l > self.container[0] or
                y + w > self.container[1] or
                z + h > self.container[2]):
            return False

        # 重量阈值约束
        if weight > self.weight_threshold and z != 0:
            return False

        # 碰撞检测
        for ox, oy, oz, ol, ow, oh in self.occupied_space:
            if (x < ox + ol and x + l > ox and
                    y < oy + ow and y + w > oy and
                    z < oz + oh and z + h > oz):
                return False

        # 物理支撑检测 (悬空限制)
        if z > Decimal('0'):
            support_area = Decimal('0')
            box_area = l * w
            for ox, oy, oz, ol, ow, oh in self.occupied_space:
                if oz + oh == z:  # 只有在箱子正下方接触时才计算
                    overlap_x = max(Decimal('0'), min(x + l, ox + ol) - max(x, ox))
                    overlap_y = max(Decimal('0'), min(y + w, oy + ow) - max(y, oy))
                    support_area += (overlap_x * overlap_y)

            # 要求至少有 60% 的底部面积被支撑
            if support_area / box_area < Decimal('0.6'):
                return False

        return True

    def place_box(self, x, y, z, l, w, h):
        """放置箱子"""
        self.packing_plan.append((x, y, z, l, w, h))
        self.occupied_space.append((x, y, z, l, w, h))

    def get_potential_points(self):
        """生成带投影的极点，极大提升空间利用率"""
        points = set([(Decimal('0'), Decimal('0'), Decimal('0'))])
        for ox, oy, oz, ol, ow, oh in self.occupied_space:
            # 基础极点
            points.add((ox + ol, oy, oz))
            points.add((ox, oy + ow, oz))
            points.add((ox, oy, oz + oh))

            # 向轴心投影的极点 (挖掘碎片空间)
            points.add((ox + ol, Decimal('0'), oz))
            points.add((ox + ol, oy, Decimal('0')))
            points.add((Decimal('0'), oy + ow, oz))
            points.add((ox, oy + ow, Decimal('0')))
            points.add((ox, Decimal('0'), oz + oh))
            points.add((Decimal('0'), oy, oz + oh))

        valid_points = []
        # 粗筛除完全处于某个箱子内部的点
        for px, py, pz in points:
            inside = False
            for ox, oy, oz, ol, ow, oh in self.occupied_space:
                if (ox < px < ox + ol) and (oy < py < oy + ow) and (oz < pz < oz + oh):
                    inside = True
                    break
            if not inside:
                valid_points.append((px, py, pz))

        return valid_points

    def pack(self):
        """使用 Best-Fit 启发式评分的装箱核心逻辑"""
        total_box_volume = sum(l * w * h * n for l, w, h, n, weight, t, i in self.boxes)
        if total_box_volume > self.container_volume:
            return False, "箱子总体积超过集装箱体积"

        # 优化排序：优先排大体积和长边箱子
        sorted_boxes = sorted(self.boxes, key=lambda x: (x[0] * x[1] * x[2], max(x[0], x[1], x[2])), reverse=True)

        box_type_count = {}
        current_box_number = 1

        for l, w, h, n, weight, t, input_order in sorted_boxes:
            box_type_count[t] = box_type_count.get(t, 0) + 1
            for _ in range(n):
                placed = False
                points = self.get_potential_points()

                best_score = (float('inf'), float('inf'), float('inf'))
                best_placement = None

                for pt_x, pt_y, pt_z in points:
                    # 使用 set 去除正方体造成的重复翻转组合
                    for perm in set(permutations([l, w, h])):
                        box_l, box_w, box_h = perm
                        if self.can_place(pt_x, pt_y, pt_z, box_l, box_w, box_h, weight):
                            # Best-Fit 评分：优先底层，其次最左，最后最前
                            score = (float(pt_z), float(pt_y), float(pt_x))
                            if score < best_score:
                                best_score = score
                                best_placement = (pt_x, pt_y, pt_z, box_l, box_w, box_h)

                if best_placement:
                    self.place_box(*best_placement)
                    self.box_colors.append({
                        'number': current_box_number,
                        'type': t,
                        'type_count': box_type_count[t],
                        'weight': float(weight),
                        'input_order': input_order,
                        'original_dimensions': (float(l), float(w), float(h))
                    })
                    current_box_number += 1
                    placed = True

                if not placed:
                    return False, f"箱子尺寸 {format_decimal(l)} * {format_decimal(w)} * {format_decimal(h)} 无法找到合适的位置"

        return True, "成功"

    def create_cuboid(self, x, y, z, dx, dy, dz):
        vertices = np.array([[x, y, z], [x + dx, y, z], [x + dx, y + dy, z], [x, y + dy, z],
                             [x, y, z + dz], [x + dx, y, z + dz], [x + dx, y + dy, z + dz], [x, y + dy, z + dz]])
        faces = [[vertices[0], vertices[1], vertices[2], vertices[3]],
                 [vertices[4], vertices[5], vertices[6], vertices[7]],
                 [vertices[0], vertices[1], vertices[5], vertices[4]],
                 [vertices[2], vertices[3], vertices[7], vertices[6]],
                 [vertices[1], vertices[2], vertices[6], vertices[5]],
                 [vertices[0], vertices[3], vertices[7], vertices[4]]]
        return faces

    def update_plot(self, box_numbers=None):
        if box_numbers:
            self.highlighted_boxes = set(box_numbers)
        else:
            self.highlighted_boxes = set()
        self.visualize()

    def visualize(self):
        """绘制集装箱和箱子的三维图（按类别高亮）"""
        plt.clf()  # 清除当前图形
        fig = plt.figure(1, figsize=(12, 8))
        ax = fig.add_subplot(111, projection='3d')

        # 将Decimal转换为float用于绘图
        container_dims = [float(x) for x in self.container]

        # 绘制集装箱边框
        container_faces = self.create_cuboid(0, 0, 0, *container_dims)
        container_box = Poly3DCollection(container_faces, alpha=0.1, facecolor='gray', edgecolor='black')
        ax.add_collection3d(container_box)

        # 使用 tab10 颜色映射
        highlight_colors = plt.cm.tab10.colors

        # 绘制每个箱子
        for idx, ((x, y, z, l, w, h), box_info) in enumerate(zip(self.packing_plan, self.box_colors)):
            # 转换为float类型
            box_coords = [float(v) for v in (x, y, z, l, w, h)]
            faces = self.create_cuboid(*box_coords)

            # 判断是否高亮：现在根据 input_order (类别) 来判断
            # input_order 从 0 开始，所以加 1 对应用户输入的 1, 2, 3...
            if (box_info['input_order'] + 1) in self.highlighted_boxes:
                # 同一类别的箱子使用同一种颜色，便于整批观察
                color_idx = box_info['input_order'] % len(highlight_colors)
                facecolor = highlight_colors[color_idx]
                alpha = 0.8
            else:
                facecolor = 'lightgray'  # 非高亮箱子使用灰色
                alpha = 0.3

            box = Poly3DCollection(faces, alpha=alpha, facecolor=facecolor, edgecolor='black')
            ax.add_collection3d(box)

            # 添加单个箱子编号（依然保留数字方便确认）
            cx = float(x) + float(l) / 2
            cy = float(y) + float(w) / 2
            cz = float(z) + float(h) / 2
            ax.text(cx, cy, cz, str(box_info['number']),
                    color='black', fontsize=10, ha='center', va='center')

        ax.view_init(elev=20, azim=45)
        ax.set_xlabel('Length')
        ax.set_ylabel('Width')
        ax.set_zlabel('Height')

        ax.set_xlim([0, container_dims[0]])
        ax.set_ylim([0, container_dims[1]])
        ax.set_zlim([0, container_dims[2]])

        ax.set_box_aspect([container_dims[0], container_dims[1], container_dims[2]])

        title = 'Container Packing Visualization'
        if self.highlighted_boxes:
            title += f'\nHighlighted Categories: {sorted(self.highlighted_boxes)}'

        plt.title(title)
        plt.tight_layout()
        plt.draw()

    def save_packing_plan(self, file_path):
        packing_plan = {
            'container': [float(x) for x in self.container],
            'boxes': [[float(x) for x in box[:3]] + [box[3], float(box[4]), box[5]] for box in self.boxes],
            'packing_plan': [
                {'position': [float(x) for x in pos[:3]], 'dimensions': [float(x) for x in pos[3:]], 'box_info': info}
                for pos, info in zip(self.packing_plan, self.box_colors)]
        }
        with open(file_path, 'w') as file:
            json.dump(packing_plan, file, indent=4)

    @staticmethod
    def load_packing_plan(file_path):
        with open(file_path, 'r') as file:
            return json.load(file)


def list_files_in_directory(extensions):
    files = [f for f in os.listdir() if any(f.endswith(ext) for ext in extensions)]
    if not files:
        print(f"当前文件夹中没有 {extensions} 文件。")
    return files


def select_file(files):
    print("请选择文件（请根据下列内容选择数据源/模型）：")
    for i, file in enumerate(files):
        print(f"{i + 1}. {file}")
    file_index = int(input("请输入文件编号: ")) - 1
    if file_index < 0 or file_index >= len(files):
        print("无效的文件编号！")
        return None
    return files[file_index]


def safe_convert_to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


def read_input_from_file(file_path):
    if file_path.endswith('.txt'):
        with open(file_path, 'r', encoding='utf-8') as file:
            lines = [line.strip() for line in file if line.strip()]

        # 兼容带有 # 号注释的数据行
        container_parts = lines[0].split('#')[0].strip().split()
        container = tuple(safe_convert_to_decimal(x) for x in container_parts[:3])
        box_count = int(lines[1].split('#')[0].strip())
        boxes = []

        for i in range(box_count):
            box_parts = lines[2 + i].split('#')[0].strip().split()
            l, w, h, n, weight = (safe_convert_to_decimal(box_parts[0]), safe_convert_to_decimal(box_parts[1]),
                                  safe_convert_to_decimal(box_parts[2]), safe_convert_to_decimal(box_parts[3]),
                                  safe_convert_to_decimal(box_parts[4]))
            t = box_parts[5]
            boxes.append((l, w, h, n, weight, t))
        return container, boxes

    elif file_path.endswith('.xlsx'):
        import openpyxl
        wb = openpyxl.load_workbook(file_path, data_only=True)
        sheet = wb.active
        container = tuple(safe_convert_to_decimal(cell.value) for cell in [cell for cell in sheet[1]][:3])
        box_count = int(sheet.cell(row=2, column=1).value)
        boxes = []
        for i in range(3, 3 + box_count):
            row = [cell.value for cell in sheet[i]]
            l, w, h, n, weight, t = (safe_convert_to_decimal(row[0]), safe_convert_to_decimal(row[1]),
                                     safe_convert_to_decimal(row[2]), safe_convert_to_decimal(row[3]),
                                     safe_convert_to_decimal(row[4]), str(row[5]))
            boxes.append((l, w, h, n, weight, t))
        return container, boxes
    else:
        raise ValueError("不支持的文件格式！")


def format_decimal(d):
    return f"{float(d):.2f}".rstrip('0').rstrip('.') if '.' in f"{float(d):.2f}" else f"{float(d):.2f}"


def calculate_file_hash(file_path):
    hasher = hashlib.md5()
    with open(file_path, 'rb') as file:
        hasher.update(file.read())
    return hasher.hexdigest()


def interactive_loop(packer):
    """按类别高亮的交互循环逻辑"""
    packer.visualize()
    plt.ion()  # 开启交互模式

    while True:
        try:
            # 采用要求的标语，明确提示是“类别编号”
            box_numbers_str = input(f"\n输入箱子类别编号以高亮显示 (共 {len(packer.boxes)} 类，输入0退出): ")
            if box_numbers_str.strip() == "0":
                break

            # 兼容英文逗号以及误输入的中文逗号
            category_numbers = [int(num.strip()) for num in box_numbers_str.replace("，", ",").split(",") if num.strip()]

            # 校验输入的数字是否在有效的类别范围内 (1 到 箱子种类数)
            if all(0 < num <= len(packer.boxes) for num in category_numbers):
                packer.update_plot(category_numbers)
            else:
                print(f"包含无效的类别编号，请重试（有效范围：1 - {len(packer.boxes)}）。")
        except ValueError:
            print("请输入有效的数字编号。")

    plt.ioff()  # 关闭交互模式
    plt.show()


def main():
    weight_threshold = float(input("请输入重量阈值（超过该重量的箱子不能叠放在其他箱子上，单位：kg）："))
    input_files = list_files_in_directory(['.txt', '.xlsx'])
    if not input_files:
        return
    input_file = select_file(input_files)
    if not input_file:
        return

    container_dims, boxes = read_input_from_file(input_file)
    packer = ContainerPacker(container_dims, boxes, weight_threshold)
    fits, result = packer.pack()

    if fits:
        # 要求1：输出“可以装入集装箱”，并按照l * w * h格式输出
        print("\n可以装入集装箱。具体装箱方案如下:")
        sorted_packing_plan = sorted(zip(packer.packing_plan, packer.box_colors), key=lambda x: x[1]['input_order'])
        for (x, y, z, l, w, h), box_info in sorted_packing_plan:
            print(f"箱子 {box_info['number']} (类型 {box_info['type']})")
            print(f"位置: ({format_decimal(x)}, {format_decimal(y)}, {format_decimal(z)})")
            print(f"尺寸: {format_decimal(l)} * {format_decimal(w)} * {format_decimal(h)}")
            print(f"重量: {format_decimal(box_info['weight'])} kg\n")

        packer.save_packing_plan("packing_plan.json")
        print("装箱方案已保存到 packing_plan.json")
        with open("data_hash.txt", "w") as hash_file:
            hash_file.write(calculate_file_hash(input_file))

        packer.visualize()
        interactive_loop(packer)
    else:
        # 要求2：输出“无法装入集装箱”，并说明原因
        print(f"\n无法装入集装箱。原因: {result}")


def load_and_visualize():
    model_files = list_files_in_directory(['.json'])
    if not model_files:
        return
    model_file = select_file(model_files)
    if not model_file:
        return

    packing_plan = ContainerPacker.load_packing_plan(model_file)
    container_dims = tuple(safe_convert_to_decimal(x) for x in packing_plan['container'])
    boxes = [(safe_convert_to_decimal(l), safe_convert_to_decimal(w), safe_convert_to_decimal(h), int(n),
              safe_convert_to_decimal(weight), t)
             for l, w, h, n, weight, t in packing_plan['boxes']]

    packer = ContainerPacker(container_dims, boxes)
    packer.packing_plan = [(safe_convert_to_decimal(x), safe_convert_to_decimal(y), safe_convert_to_decimal(z),
                            safe_convert_to_decimal(l), safe_convert_to_decimal(w), safe_convert_to_decimal(h))
                           for x, y, z, l, w, h in
                           [item['position'] + item['dimensions'] for item in packing_plan['packing_plan']]]
    packer.box_colors = [item['box_info'] for item in packing_plan['packing_plan']]

    print("\n可以装入集装箱。具体装箱方案如下:")
    for i, ((x, y, z, l, w, h), box_info) in enumerate(zip(packer.packing_plan, packer.box_colors)):
        print(f"箱子 {box_info['number']} (类型 {box_info['type']})")
        print(f"位置: ({format_decimal(x)}, {format_decimal(y)}, {format_decimal(z)})")
        print(f"尺寸: {format_decimal(l)} * {format_decimal(w)} * {format_decimal(h)}")
        print(f"重量: {format_decimal(box_info['weight'])} kg\n")

    packer.visualize()
    interactive_loop(packer)


if __name__ == "__main__":
    if os.path.exists("packing_plan.json"):
        choice = input("检测到保存的装箱方案，请选择:\n1. 使用已有模型\n2. 重新运行并保存新模型\n请输入选项 (1 或 2): ")
        if choice == "1":
            load_and_visualize()
        elif choice == "2":
            print("重新计算装箱方案...")
            main()
        else:
            print("无效选项，程序退出。")
    else:
        print("未找到保存的装箱方案，开始计算装箱方案...")
        main()
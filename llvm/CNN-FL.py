import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader
from configparser import ConfigParser
from collections import defaultdict


# ==========================================
# 1. 严格还原论文的 CNN-FL 网络结构 (防爆炸版)
# ==========================================
class CNN_FL_Model(nn.Module):
    def __init__(self, num_statements):
        super(CNN_FL_Model, self).__init__()
        self.num_statements = num_statements

        self.conv1 = nn.Conv1d(in_channels=1, out_channels=32, kernel_size=10, padding=5)
        self.relu1 = nn.ReLU()
        self.pool1 = nn.MaxPool1d(kernel_size=2)

        self.conv2 = nn.Conv1d(in_channels=32, out_channels=64, kernel_size=10, padding=5)
        self.relu2 = nn.ReLU()
        self.pool2 = nn.MaxPool1d(kernel_size=2)

        flat_size = self._get_conv_output_size(num_statements)

        self.fc1 = nn.Linear(flat_size, 1024)
        self.fc2 = nn.Linear(1024, 1024)
        self.fc3 = nn.Linear(1024, 1024)
        self.out = nn.Linear(1024, 1)

        # 【修改1：去掉了 Sigmoid】交由更稳定的 BCEWithLogitsLoss 在底层处理

    def _get_conv_output_size(self, shape):
        bs = 1
        dummy_input = torch.zeros(bs, 1, shape)
        x = self.pool1(self.relu1(self.conv1(dummy_input)))
        x = self.pool2(self.relu2(self.conv2(x)))
        return x.numel() // bs

    def forward(self, x):
        x = x.unsqueeze(1)

        x = self.pool1(self.relu1(self.conv1(x)))
        x = self.pool2(self.relu2(self.conv2(x)))

        x = x.view(x.size(0), -1)  # 严格展平，保留位置特征

        x = torch.relu(self.fc1(x))
        x = torch.relu(self.fc2(x))
        x = torch.relu(self.fc3(x))

        x = self.out(x)
        # 【修改2：直接返回未经 Sigmoid 压缩的原始 Logits】
        return x


# ==========================================
# 2. 核心主函数
# ==========================================
def CNN_FL(bugid, configFile):
    print(f"=== 开始处理 Bug ID: {bugid} ===")

    cfg = ConfigParser()
    cfg.read(configFile)
    baseCaseDir = cfg.get('llvm-locations', 'passdir')
    failcovDir = os.path.join(baseCaseDir, bugid, 'failcov')
    passcovDir = os.path.join(baseCaseDir, bugid, 'passcov')

    fail_statements = set()
    file_stmt_count = defaultdict(set)

    def collect_fail_statements(cov_dir):
        if not os.path.exists(cov_dir): return
        subdirs = [os.path.join(cov_dir, d) for d in os.listdir(cov_dir) if os.path.isdir(os.path.join(cov_dir, d))]
        for subdir in subdirs:
            stmt_file = os.path.join(subdir, 'stmt_info.txt')
            if not os.path.exists(stmt_file): continue
            with open(stmt_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    parts = line.split(':')
                    if len(parts) == 2:
                        filepath = parts[0]
                        for l in parts[1].split(','):
                            stmt_key = f"{filepath}:{l}"
                            fail_statements.add(stmt_key)
                            file_stmt_count[filepath].add(l)

    print(">> 正在提取失败用例的语句特征...")
    collect_fail_statements(failcovDir)
    if not fail_statements: return None

    target_stmts = sorted(list(fail_statements))
    stmt_to_index = {stmt: idx for idx, stmt in enumerate(target_stmts)}
    num_features = len(target_stmts)
    print(f">> 提取的失败语句总数 (N) 为: {num_features}")

    coverage_matrix = []
    error_vector = []

    def build_matrix(cov_dir, is_fail):
        if not os.path.exists(cov_dir): return
        subdirs = [os.path.join(cov_dir, d) for d in os.listdir(cov_dir) if os.path.isdir(os.path.join(cov_dir, d))]
        for subdir in subdirs:
            stmt_file = os.path.join(subdir, 'stmt_info.txt')
            if not os.path.exists(stmt_file): continue
            vector = [0] * num_features
            with open(stmt_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    parts = line.split(':')
                    if len(parts) == 2:
                        filepath = parts[0]
                        for l in parts[1].split(','):
                            stmt_key = f"{filepath}:{l}"
                            if stmt_key in stmt_to_index:
                                vector[stmt_to_index[stmt_key]] = 1
            coverage_matrix.append(vector)
            error_vector.append(1 if is_fail else 0)

    print(">> 正在构建覆盖率矩阵...")
    build_matrix(failcovDir, is_fail=True)
    build_matrix(passcovDir, is_fail=False)

    num_samples = len(coverage_matrix)
    print(f">> 矩阵构建完成: 大小为 {num_samples} (M) x {num_features} (N)")

    print("\n>> 开始训练 CNN-FL 模型...")
    X = torch.tensor(coverage_matrix, dtype=torch.float32)
    y = torch.tensor(error_vector, dtype=torch.float32).view(-1, 1)

    dataset = TensorDataset(X, y)
    dataloader = DataLoader(dataset, batch_size=10, shuffle=True)

    model = CNN_FL_Model(num_statements=num_features)

    # 【修改3：使用结合了 Sigmoid 且数值极度稳定的 Loss 函数】
    criterion = nn.BCEWithLogitsLoss()

    # 【修改4：将学习率降低 10 倍 (1e-4)，并加入 weight_decay (L2正则化) 防暴走】
    optimizer = optim.Adam(model.parameters(), lr=1e-4, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.95)

    epochs = 30
    model.train()
    for epoch in range(epochs):
        epoch_loss = 0.0
        for batch_X, batch_y in dataloader:
            optimizer.zero_grad()
            outputs = model(batch_X)
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()

        scheduler.step()
        if (epoch + 1) % 10 == 0:
            print(f"   Epoch [{epoch + 1}/{epochs}], Loss: {epoch_loss / len(dataloader):.4f}")

    model.eval()
    print("\n>> 开始计算语句和文件的可疑度 (分批推理中)...")

    suspiciousness_scores = []
    eval_batch_size = 64

    with torch.no_grad():
        for i in range(0, num_features, eval_batch_size):
            end_idx = min(i + eval_batch_size, num_features)
            current_batch_size = end_idx - i

            batch_virtual_test = torch.zeros(current_batch_size, num_features, dtype=torch.float32)
            for j in range(current_batch_size):
                batch_virtual_test[j, i + j] = 1.0

            batch_logits = model(batch_virtual_test)
            # 【修改5：在推理结果出来后，手动施加 sigmoid 转为 0~1 的概率分数】
            batch_scores = torch.sigmoid(batch_logits).view(-1).numpy()
            suspiciousness_scores.extend(batch_scores)

    file_scores_sum = defaultdict(float)
    file_stmts_count = defaultdict(int)

    for i, stmt in enumerate(target_stmts):
        filepath = stmt.split(':')[0]
        score = float(suspiciousness_scores[i])
        file_scores_sum[filepath] += score
        file_stmts_count[filepath] += 1

    file_avg_suspiciousness = {}
    for filepath, total_score in file_scores_sum.items():
        file_avg_suspiciousness[filepath] = total_score / file_stmts_count[filepath]

    ranked_files = sorted(file_avg_suspiciousness.items(), key=lambda item: item[1], reverse=True)

    print("\n" + "=" * 100)
    print(" 最终文件可疑度排名 (Top 20)")
    print("=" * 100)
    print(f"{'排名':<4} | {'平均可疑度':<12} | {'包含语句数':<8} | {'文件路径'}")
    print("-" * 100)

    for rank, (filepath, avg_score) in enumerate(ranked_files[:20], start=1):
        count = file_stmts_count[filepath]
        print(f"{rank:<6} | {avg_score:.6f}     | {count:<10} | {filepath}")

    print("=" * 100)
    return ranked_files


if __name__ == "__main__":
    CNN_FL('15920', '/home/chris/ghoti/config/config.ini')
    pass
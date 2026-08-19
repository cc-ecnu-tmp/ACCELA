# ACCELA Tensor 纯前端适配与离线复刻指南

本文以 `zeyi2/vector` 的 `9b3d73ab` 为基线，说明提交 `3ce364fe` 的最终实现。目标是在不修改 `Ty`、解释器、IR、优化 Pass 和后端的前提下，把最高二维的 Tensor 语法降为编译器已经支持的数组与 vector AST。

本文档是当前方案的唯一说明来源。旧的“Tensor 作为正式 `Ty` 贯穿到 AST2IR”方案、隐藏首维长度方案以及三维以上方案均已废弃。

## 1. 最终语义边界

支持一维和二维声明：

```java
tensor int a[4] = {1, 2};
tensor float b[2][3] = {{1.0, 2.0, 3.0}, {4.0, 5.0, 6.0}};
const tensor int c[2][2] = {1, 2, 3, 4};
```

函数形参采用 C 数组指针风格，首维必须为空，且不传隐藏长度：

```java
int consume1(tensor int a[]);
int consume2(tensor float a[][8]);
```

以下声明必须拒绝：

```java
int bad1(tensor int a[4]);       // 形参首维不能固定
int bad2(tensor int a[2][3]);    // 形参首维不能固定
int bad3(tensor int a[][2][3]);  // 最高只支持二维
tensor int x[2][3][4];           // 最高只支持二维
```

支持完整下标、元素写回、固定 Tensor 整体赋值、逐元素运算、标量广播和二维矩阵乘：

```java
a[i] = a[i] + 1;
b[i][j] = b[i][j] * 2.0;
fixed = fixed + 1;
fixed = parameter + 1;  // fixed 的形状给出循环范围
c = a + b;
c = 2 * a - b / 4;
c = a @ b;
```

逐元素 Tensor-Tensor 运算要求维数和各维长度一致；`int` 与 `float` 按现有 vector 数值提升规则得到 `float`。`%` 只允许整数。`@` 与 `* / %` 同优先级并保持左结合。

部分索引、Tensor 条件、比较、逻辑运算、三维 Tensor 和高维 `@` 均立即报错。

## 2. 最终编译管线

```text
source
  -> Lexer / Parser
       识别 tensor、@，在 Node 上留下临时标记和 TensorShape
  -> Sema.analyze 入口
       TensorLowering 把 Tensor AST 改写为普通 array/vector AST
       清除全部 Tensor 临时标记
  -> 原有 Sema
  -> 原有 AST2IR
  -> 原有 vector Pass / scalarization
  -> 原有 RV64GC 后端
```

降级属于 Sema 前置前端 Pass，而不是 `Compiler` 中的可选旁路。降级结束后，下游看到的只有已有的标量、数组和 vector：

```text
Compiler               不引用 TensorLowering
Sema                    只在 analyze 入口调用 TensorLowering
Interpreter             没有 Tensor 专用分支
AST2IR                  没有 Tensor 专用分支
IR / Pass / Backend     没有 Tensor 专用类型或指令
```

## 3. 物理表示与 ABI

| 源语言类型 | 降级后的前端表示 | 说明 |
|---|---|---|
| `tensor T a[N]` | `vector T<N> a[1]` | 一个 vector 容器 |
| `tensor T a[M][N]` | `vector T<N> a[M]` | M 个 N-lane 行 vector |
| `tensor T a[]` 形参 | `T a[]` | 标量指针，无隐藏长度 |
| `tensor T a[][N]` 形参 | `vector T<N> a[]` | vector 行指针，无隐藏长度 |
| 固定 Tensor 返回 | `void` + 第一个隐藏输出指针 | 返回前完整复制 |

固定一维仍使用“长度为 1 的 vector 行数组”，从而让声明、临时值、复制和返回缓冲区复用同一套固定存储逻辑。

动态一维参数必须视为标量指针，因为函数签名中没有 lane 数。动态二维参数的列数 `N` 固定，因此可以直接视为 vector 行指针。

## 4. Diff 总览

提交 `3ce364fe` 相对 `zeyi2/vector`：

```text
4 files changed, 101 insertions(+), 236 deletions(-)
```

| 文件 | 作用 |
|---|---|
| `src/main/java/Compiler.java` | 删除 Compiler 层 Tensor 旁路 |
| `src/main/java/accela/parse/Sema.java` | 把降级固定接入 Sema 入口 |
| `src/main/java/accela/ast/TensorShape.java` | 把形状限制为一维或二维 |
| `src/main/java/accela/preprocess/TensorLowering.java` | 删除隐藏长度和动态目标专用逻辑，复用现有 vector |

提交不包含测试、PDF、IDE 状态或现场临时文件。

## 5. Diff 块一：把 Pass 接入 Sema

### 5.1 Compiler 删除条件调用

`Compiler` 只保留统一的 Parser → Sema 路径：

```java
String source = new String(Files.readAllBytes(Paths.get(compileArgument.inputFilePath())));
Node unit = parseSyntax(source, compileArgument.inputFilePath());
new Sema().analyze(unit);
```

删除旧 import 和条件旁路：

```java
// 删除 import accela.preprocess.TensorLowering;

// 删除 Compiler.main 中的代码：
if (TensorLowering.isRequired(unit)) {
  TensorLowering.lower(unit, compileArgument.inputFilePath());
}
```

这样所有调用 `Sema.analyze` 的入口都会执行相同降级，测试工具或其它驱动不会因为绕过 `Compiler.main` 而漏掉 Tensor Pass。

### 5.2 Sema 入口固定执行降级

```java
import accela.preprocess.TensorLowering;

public void analyze(Node unit) {
  if (TensorLowering.isRequired(unit)) TensorLowering.lower(unit, "<frontend>");
  for (Node child : unit.kids) {
    if (child.tag == FUNC) analyzeFuncDef(child);
    else if (child.tag == VAR) analyzeVarDecl(child);
  }
}
```

`isRequired` 只决定是否跳过一次无意义的 AST 遍历，不改变语言语义。真正入口固定在 Sema。

降级最后清除标记，因此原有 Sema 不需要理解 Tensor：

```java
private void clearTensorMarkers(Node node) {
  node.tensor = false;
  node.tensorMatmul = false;
  if (node.dimExprs != null) node.dimExprs.forEach(this::clearTensorMarkers);
  node.kids.forEach(this::clearTensorMarkers);
}
```

## 6. Diff 块二：TensorShape 限制为最高二维

Tensor 形状只允许 `[N]`、`[M][N]`、参数 `[]` 和参数 `[][N]`。动态维使用 `DYNAMIC = -1`，且只能位于第一维。

```java
public TensorShape(Ty element, int... dimensions) {
  if (element != Ty.INT && element != Ty.FLOAT)
    throw new IllegalArgumentException("Tensor element type must be int or float");
  if (dimensions.length < 1 || dimensions.length > 2)
    throw new IllegalArgumentException("Tensor rank must be one or two");
  if (dimensions[0] == 0 || dimensions[0] < DYNAMIC
      || dimensions.length == 2 && dimensions[1] <= 0)
    throw new IllegalArgumentException(
        "Tensor dimensions must be positive except parameter dim0");
  this.element = element;
  this.dimensions = dimensions.clone();
}
```

表达式和赋值使用动态首维通配的全维兼容检查：

```java
public boolean compatibleDimensions(TensorShape other) {
  if (other == null || rank() != other.rank()) return false;
  for (int i = 0; i < dimensions.length; i++) {
    int left = dimensions[i], right = other.dimensions[i];
    if (left != DYNAMIC && right != DYNAMIC && left != right) return false;
  }
  return true;
}
```

函数传参还要检查元素类型和尾维：

```java
public boolean sameTrailingDimensions(TensorShape other) {
  if (other == null || element != other.element || rank() != other.rank()) return false;
  for (int i = 1; i < dimensions.length; i++) {
    if (dimensions[i] != other.dimensions[i]) return false;
  }
  return true;
}
```

记忆：`compatibleDimensions` 用于表达式和赋值；`sameTrailingDimensions` 用于指针形参。

## 7. Diff 块三：声明和形参

固定变量只允许一维或二维正常量：

```java
private TensorShape fixedShape(Node declaration) {
  if (declaration.dimExprs == null
      || declaration.dimExprs.isEmpty() || declaration.dimExprs.size() > 2)
    fail("Tensor " + declaration.s + " must have shape [N] or [M][N]");
  int[] dimensions = new int[declaration.dimExprs.size()];
  for (int i = 0; i < dimensions.length; i++) {
    Integer value = evalConst(declaration.dimExprs.get(i));
    if (value == null || value <= 0)
      fail("Tensor " + declaration.s + " dimensions must be positive constants");
    dimensions[i] = value;
  }
  return new TensorShape(declaration.ty, dimensions);
}
```

形参只允许 `[]` 或 `[][N]`：

```java
private TensorShape parameterShape(Node parameter) {
  if (!parameter.flag)
    fail("Tensor parameter " + parameter.s + " must omit its first dimension");
  int trailing = parameter.dimExprs == null ? 0 : parameter.dimExprs.size();
  if (trailing > 1)
    fail("Tensor parameter " + parameter.s + " must have form [] or [][N]");
  if (trailing == 0)
    return new TensorShape(parameter.ty, TensorShape.DYNAMIC);
  Integer columns = evalConst(parameter.dimExprs.getFirst());
  if (columns == null || columns <= 0)
    fail("Tensor parameter " + parameter.s
        + " requires a positive constant column count");
  return new TensorShape(parameter.ty, TensorShape.DYNAMIC, columns);
}
```

不要添加隐藏 `dim0`。函数降级只改变数据参数类型：

```java
parameter.ty = shape.rank() == 1
    ? shape.element
    : Ty.vector(shape.element, shape.last());
parameter.flag = true;
parameter.dimExprs = new ArrayList<>();
parameter.tensor = false;
parameters.add(parameter);
```

对应关系：

```text
tensor int a[]       -> int a[]
tensor float b[][8]  -> vector float<8> b[]
```

## 8. Diff 块四：存储分类，不携带长度

只区分 vector 行和动态一维标量指针：

```java
private enum Storage {
  VECTOR_ROWS,
  DYNAMIC_SCALARS
}

private record TensorSymbol(
    String name,
    TensorShape shape,
    boolean constant,
    Storage storage) {}
```

```java
private TensorSymbol parameterSymbol(String name, TensorShape shape) {
  return new TensorSymbol(
      name,
      shape,
      false,
      shape.rank() == 1 ? Storage.DYNAMIC_SCALARS : Storage.VECTOR_ROWS);
}
```

旧的 `extentName`、隐藏长度参数、`extent()` 和动态行数计算全部删除。没有长度的动态 Tensor 不能自行决定整体循环次数：

```java
private Node rowCount(TensorSymbol symbol) {
  if (symbol.shape.dynamic())
    fail("dynamic Tensor row count requires a fixed-shape destination");
  return Node.intLiteral(symbol.shape.rows());
}
```

## 9. Diff 块五：固定存储复用 vector

末维是 vector lane，其余维度乘积是 vector 行数：

```java
private void lowerFixedStorage(Node declaration, TensorShape shape) {
  declaration.ty = Ty.vector(shape.element, shape.last());
  declaration.dimExprs = new ArrayList<>(
      List.of(Node.intLiteral(shape.rows())));
  declaration.tensor = false;
}
```

```text
tensor int a[4]       -> int4 a[1]
tensor float b[3][8]  -> float8 b[3]
```

`int4` 和 `float8` 只是结果说明。实际实现必须使用 `Ty.vector(element, lanes)`，不枚举 `int4/int8/int16`，从而让任意正常量尾维继续走现有 vector 自动推导和标量化。

初始化先按 C/SysY 规则铺平成标量，再组成 vector 行；缺失元素填零，嵌套和平铺初始化得到相同布局。

## 10. Diff 块六：固定目标决定动态表达式范围

没有隐藏长度后，参数整体表达式只有放入固定目标时才有确定范围：

```java
private TensorValue lowerTensor(
    Node expression, Scope scope, TensorShape preferred) {
  TensorShape inferred = shapeOf(expression, scope);
  if (inferred == null) fail("expected Tensor expression");
  TensorShape shape = inferred;
  if (preferred != null && !inferred.compatibleDimensions(preferred)) {
    fail("Tensor expression " + inferred
        + " does not fit destination " + preferred);
  }
  if (shape.dynamic()) {
    if (preferred == null || preferred.dynamic())
      fail("dynamic Tensor expression requires a fixed-shape destination");
    shape = preferred;
  }
  // 继续按 REF、CALL、UNARY、BIN、@ 分派。
}
```

合法：

```java
int f(tensor int a[]) {
  tensor int result[8] = a + 1;
  return result[0];
}
```

非法：

```java
int f(tensor int a[]) {
  a = a + 1;  // a 没有运行时长度
  return 0;
}
```

动态参数整体赋值立即拒绝；元素写回仍然合法：

```java
if (target.shape.dynamic())
  fail("whole assignment to Tensor parameter requires an explicit fixed-shape destination");
```

```java
int update(tensor int a[]) {
  a[3] = a[3] + 1;
  return a[3];
}
```

## 11. Diff 块七：动态一维与动态二维

动态二维已经是 vector 行指针，可以直接复用；动态一维是标量指针，需要在固定目标给出 lane 数后物化：

```java
if (expression.tag == REF) {
  TensorSymbol source = scope.require(expression.s);
  if (source.storage == Storage.VECTOR_ROWS)
    return new TensorValue(List.of(), source);

  TensorSymbol result = temporary(shape);
  List<Node> code = new ArrayList<>();
  code.add(declarationStatement(temporaryDeclaration(result)));
  code.addAll(copy(result, source));
  return new TensorValue(code, result);
}
```

动态一维复制的循环上界来自固定目标：

```java
if (target.shape.rank() == 1
    && source.storage == Storage.DYNAMIC_SCALARS) {
  String index = fresh("copy_lane");
  return List.of(loop(
      index,
      Node.intLiteral(target.shape.last()),
      assign(scalarAt(target, ref(index)), scalarAt(source, ref(index)))));
}
```

这比给 ABI 增加长度参数更容易现场复刻，并让固定运算继续走 vector。

## 12. Diff 块八：逐元素运算和广播

Tensor-Tensor 先检查全维兼容；固定形状优先作为结果形状，动态首维只充当通配符：

```java
if (left != null && right != null
    && !left.compatibleDimensions(right))
  fail("Tensor dimensions must match");

TensorShape basis = left != null && !left.dynamic() ? left
    : right != null && !right.dynamic() ? right
    : left != null ? left : right;
```

元素类型复用现有数值提升：

```java
Ty element = basis.element;
if (left != null && right != null
    && (left.element == Ty.FLOAT || right.element == Ty.FLOAT)) {
  element = Ty.FLOAT;
} else {
  Node scalar = left == null ? expression.kids.get(0)
      : right == null ? expression.kids.get(1) : null;
  if (scalar != null && scalarType(scalar, scope) == Ty.FLOAT)
    element = Ty.FLOAT;
}
```

每个物理行只生成一个已有 vector 运算：

```java
Node lhs = leftTensor == null
    ? leftScalar.expression
    : row(leftTensor.storage, ref(index));
Node rhs = rightTensor == null
    ? rightScalar.expression
    : row(rightTensor.storage, ref(index));

code.add(loop(
    index,
    rowCount(result),
    assign(
        row(result, ref(index)),
        binary(expression.op, lhs, rhs))));
```

标量广播无需 Tensor 专用 splat；已有 vector Sema/IR 会把标量扩展到所有 lane。

## 13. Diff 块九：二维矩阵乘

`@` 只接受二维、相同元素类型的 Tensor，并要求固定结果：

```java
if (a.rank() != 2 || b.rank() != 2 || resultShape.rank() != 2)
  fail("@ is only defined for rank-two Tensors");
if (resultShape.dynamic())
  fail("@ with a pointer-style Tensor requires a fixed-shape destination");
if (a.element != b.element
    || resultShape.element != a.element
    || resultShape.dimensions[1] != b.dimensions[1]
    || b.dimensions[0] != TensorShape.DYNAMIC
        && a.dimensions[1] != b.dimensions[0]) {
  fail("@ requires same-type A[M][K] and B[K][N]");
}
```

固定结果 `[M][N]` 和左操作数尾维 `K` 补全本次动态参数运算的形状：

```java
TensorShape leftShape = new TensorShape(
    a.element, resultShape.dimensions[0], a.dimensions[1]);
TensorShape rightShape = new TensorShape(
    b.element, a.dimensions[1], b.dimensions[1]);
```

核心计算直接复用 N-lane vector：

```java
Node accumulate = assign(
    row(result, ref(i)),
    binary(
        ADD,
        row(result, ref(i)),
        binary(
            MUL,
            row(right.storage, ref(k)),
            element(left.storage, ref(i), ref(k)))));

Node inner = loop(k, Node.intLiteral(a.dimensions[1]), accumulate);
Node body = block(
    assign(row(result, ref(i)), zero(resultShape.element)),
    inner);
code.add(loop(i, Node.intLiteral(resultShape.dimensions[0]), body));
```

即：

```text
result[i][:] = 0
for k in [0, K):
  result[i][:] += B[k][:] * A[i][k]
```

`B[k][:]` 和结果行都是 N-lane vector，`A[i][k]` 是标量，由已有广播处理。复杂操作数先物化，因此调用、嵌套表达式和别名目标都只求值一次。

## 14. Diff 块十：函数调用没有隐藏实参

Tensor 参数只传数据地址：

```java
TensorValue value = argument.tag == REF
    ? new TensorValue(List.of(), scope.require(argument.s))
    : lowerTensor(argument, scope, actual.dynamic() ? null : actual);
prefix.addAll(value.prefix);
arguments.add(ref(value.storage.name));
```

删除以下旧机制：

```text
隐藏 dim0 实参
固定一维参数适配数组
调用后 copy-back
动态参数 extent 转发
动态目标原地逐元素循环
动态目标原地矩阵乘
```

指针写回由普通数组参数自然完成：

```java
int update(tensor int a[], tensor int rows[][2]) {
  a[1] = 7;
  rows[1][1] = 9;
  return 0;
}
```

## 15. Tensor 返回值

返回形状从所有 `return tensorExpr` 推导，并要求元素类型一致、形状固定、所有路径完全相同。不能直接返回仍为动态首维的参数。

函数降为 `void`，第一个参数是隐藏输出缓冲区：

```java
if (info.returnShape != null) {
  String resultName = hidden(function.s, "result");
  Node result = arrayParameter(
      resultName,
      Ty.vector(info.returnShape.element, info.returnShape.last()));
  parameters.add(result);
  scope.put(new TensorSymbol(
      resultName, info.returnShape, false, Storage.VECTOR_ROWS));
  function.ty = Ty.VOID;
}
```

`return` 先完整复制，再生成无值返回：

```java
TensorValue value = lowerTensor(
    statement.kids.getFirst(), scope, function.returnShape);
TensorSymbol output = new TensorSymbol(
    hidden(function.source.s, "result"),
    function.returnShape,
    false,
    Storage.VECTOR_ROWS);
List<Node> code = new ArrayList<>(value.prefix);
code.addAll(copy(output, value.storage));
code.add(new Node(RET));
```

这允许返回局部 Tensor，也允许返回值继续下标、运算、传参或参与 `@`。

## 16. 下标映射

```text
一维固定       a[i]    -> a[0][i]
一维动态形参   a[i]    -> a[i]
二维固定/动态  a[i][j] -> a[i][j]
```

实现通过存储类别选择标量指针或 vector 行。索引数量必须等于 Tensor rank；部分索引不产生低阶 Tensor：

```java
if (indices.size() != symbol.shape.rank())
  fail("partial Tensor indexing is not defined for " + symbol.shape);
```

## 17. Fail-fast 清单

以下情况必须在 TensorLowering 阶段失败：

- 固定 Tensor 没有维度、维度不是正常量、为零或 rank 大于 2；
- 形参首维不是空 `[]`，或存在两个及以上尾维；
- Tensor-Tensor 逐元素运算 rank 或维度不一致；
- float Tensor 使用 `%`；
- Tensor 参与比较、逻辑运算、条件或逻辑非；
- 部分索引或索引数量过多；
- `@` 任一操作数不是二维；
- `@` 元素类型不同、K 不匹配或结果列数不匹配；
- 动态整体表达式没有固定目标提供形状；
- 对动态参数做整体赋值；
- Tensor 返回形状动态、缺失或不同路径不一致；
- 修改 const Tensor。

禁止静默截断、补隐藏长度或回退到另一套标量语义。

## 18. 为什么这是最小且可记忆的方案

方案没有新增 Tensor `Ty.Kind`、IR Type、IR Instruction 或后端指令。现场只需记住两个临时概念：

```text
TensorShape     前端临时形状
Storage         VECTOR_ROWS / DYNAMIC_SCALARS
```

固定计算全部变成已有 vector 运算：

```text
一维固定       1 个 vector 行
二维固定       M 个 vector 行
逐元素         每行 1 个 vector op
广播           现有 scalar-to-vector splat
矩阵乘         B 行 vector * A 标量 lane
返回           固定 vector 行复制
后端           现有 vector scalarization
```

相较旧版本，当前提交在 `TensorLowering` 中删除 220 行、增加 92 行，主要删掉隐藏长度、动态目标专用运算、参数适配和 suffix/copy-back 状态。

## 19. 离线复刻顺序

如果现场仓库已经包含 `zeyi2/vector` 的 Tensor 语法骨架：

1. 在 `Compiler` 删除 `TensorLowering` import 和条件调用。
2. 在 `Sema.analyze` 第一行调用 `TensorLowering.lower`。
3. 把 `TensorShape` rank 限制为 1..2，并加入动态通配的全维兼容检查。
4. 把形参限制为 `[]` 或 `[][N]`。
5. 删除隐藏 `dim0` 参数、实参、字段和 extent 计算。
6. rank1 参数降为标量数组；rank2 参数降为 vector 行数组。
7. 禁止动态参数整体赋值；动态整体表达式要求固定目标。
8. 动态 rank1 按固定目标物化；动态 rank2 直接复用 vector 行。
9. 把 `@` 限制为二维，并让固定结果补全动态 M。
10. 删除 suffix、copy-back 和动态原地运算代码。
11. 确认 Interpreter、AST2IR、IR 和 Backend 没有 Tensor 专用分支。
12. 编译、跑聚焦回归，再跑 RV64GC 裸机用例。

如果现场仓库尚无 Tensor 语法骨架，还需先完成：

1. Lexer 增加 `AT`，把 `tensor` 识别为类型起始词。
2. Parser 在 `tensor` 后只接受 `int` 或 `float`。
3. Parser 在函数返回、形参和变量声明节点设置 `node.tensor`。
4. Parser 把 `@` 放入乘法级优先级，映射到 `Op.MUL`，同时设置 `node.tensorMatmul`。
5. Node 增加 `tensor`、`tensorShape` 和 `tensorMatmul` 三个临时字段。
6. AST2IR 的零首维数组参数 GEP 使用元素类型推导 stride，使 vector 行数组参数可寻址。

基础语法骨架对应 `1a010efe`；最终收紧和纯前端接入对应 `3ce364fe`。

## 20. 最小验证矩阵

| 类别 | 必测行为 |
|---|---|
| 一维声明 | 部分初始化补零、平铺初始化、const 读取 |
| 二维声明 | 嵌套与平铺初始化布局一致 |
| 一维参数 | 元素读取、写回、参数转发 |
| 二维参数 | 动态行索引、写回、参数转发 |
| 固定目标 | `fixed = parameter + scalar` |
| 逐元素 | int/float、左右标量、链式、别名目标 |
| 返回值 | 局部返回、调用结果下标、继续运算和传参 |
| 矩阵乘 | 方阵、矩形、动态参数、别名目标 |
| 后端 | IR verifier、汇编生成、RV64GC 实际运行 |

至少验证这些拒绝程序：

```java
tensor int rank3[2][3][4];
int fixed_first(tensor int a[4]);
int fixed_first_2d(tensor int a[2][3]);
int rank3_param(tensor int a[][2][3]);

int dynamic_whole(tensor int a[]) {
  a = a + 1;
  return 0;
}

int partial() {
  tensor int a[2][3] = {};
  return a[1];
}

int bad_matmul() {
  tensor int a[4] = {};
  tensor int b[4] = {};
  tensor int c[4] = a @ b;
  return 0;
}
```

当前聚焦证据：

- 10 个 RV64GC/QEMU 正向程序均返回 0；
- 6 个非法程序均非零失败；
- VectorFrontend、VectorScalarization、VectorIR、AST2IR 初始化、RISCVCallAbi 和 RISCVGEP 等聚焦回归通过。

这些是聚焦正确性证据，不代表全量测试或 BOOM 性能结论。

## 21. 跨平台复核命令

```sh
git show --stat 3ce364fe
git show 3ce364fe -- \
  src/main/java/Compiler.java \
  src/main/java/accela/parse/Sema.java \
  src/main/java/accela/ast/TensorShape.java \
  src/main/java/accela/preprocess/TensorLowering.java
```

检查 Tensor 是否泄漏到下游：

```sh
rg -n "Tensor|tensor" \
  src/main/java/accela/ir \
  src/main/java/accela/backend \
  src/main/java/accela/ast/Interpreter.java
```

编译和聚焦测试：

```sh
./gradlew compileJava
./gradlew test \
  --tests accela.parse.VectorFrontendTest \
  --tests accela.pass.ir.transform.VectorScalarizationTest \
  --tests accela.backend.RISCVCallAbiTest \
  --tests accela.backend.RISCVGEPTest
```

如果工作树中保留了旧方案的未跟踪 `TensorFrontendTest`，它会因仍引用 `Ty.tensor` 和 `Op.MATMUL` 而阻塞测试源编译。应更新为“降级后只存在 array/vector AST”的断言，不能为了兼容旧测试而把 Tensor 重新引入 `Ty` 或 IR。

## 22. 现场记忆法

记住八个字：

```text
二、行、指、定、算、乘、返、清
```

- **二**：最高二维，一维和二维都支持。
- **行**：末维是 vector lane，外层是 vector 行。
- **指**：形参是指针，`[]` 或 `[][N]`，没有隐藏长度。
- **定**：动态整体运算必须由固定目标给出范围。
- **算**：逐元素按 vector 行，标量使用已有广播。
- **乘**：`@` 只二维，用 B 行乘 A lane 后累加。
- **返**：固定形状，隐藏输出缓冲区复制。
- **清**：进入原 Sema 前清掉所有 Tensor 标记。

再记住四个文件：

```text
Compiler      删旁路
Sema          接入口
TensorShape   限二维
Lowering      去长度、复用 vector
```

## 23. 最短背诵版

> Tensor 只活到 Sema 入口；一维是一个 vector 行，二维是多个 vector 行。  
> 参数只有 `[]` 和 `[][N]`，按指针传递且没有隐藏长度。  
> 动态整体运算必须写入固定目标；逐元素复用 vector，`@` 用 B 行乘 A lane。  
> 固定返回走隐藏输出缓冲区；降级后 Sema、IR 和后端完全看不到 Tensor。

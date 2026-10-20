"""Generate deterministic offline Maven benchmark fixtures and Git bundles."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_DIR = PROJECT_ROOT / "evaluation"
FIXTURES_DIR = EVALUATION_DIR / "fixtures"
TASKS_FILE = EVALUATION_DIR / "tasks.json"

FIXED_DATE = "2024-01-01T00:00:00+0000"


def pom(artifact_id: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>dev.java-code-agent.benchmark</groupId>
  <artifactId>{artifact_id}</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.source>8</maven.compiler.source>
    <maven.compiler.target>8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.5.4</version>
      </plugin>
    </plugins>
  </build>
</project>
"""


FIXTURES = [
    {
        "task_id": "arithmetic-logic",
        "title": "Arithmetic logic error",
        "description": "Order arithmetic helper returns the wrong sum.",
        "problem_statement": "订单金额的加法计算不正确。请定位并修复实现，确保现有 Maven 测试通过。",
        "tags": ["arithmetic", "simple"],
        "files": {
            "src/main/java/fixture/Calculator.java": """package fixture;

public class Calculator {
    public int add(int left, int right) {
        return left - right;
    }
}
""",
            "src/test/java/fixture/CalculatorTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CalculatorTest {
    @Test
    public void addsPositiveNumbers() {
        assertEquals(5, new Calculator().add(2, 3));
    }

    @Test
    public void addsZero() {
        assertEquals(0, new Calculator().add(0, 0));
    }
}
""",
        },
    },
    {
        "task_id": "condition-boundary",
        "title": "Condition boundary error",
        "description": "Free shipping policy uses the wrong threshold and premium condition.",
        "problem_statement": "免运费判断在普通大额订单和会员订单上不正确。请修复条件逻辑并确保测试通过。",
        "tags": ["condition", "business-rule"],
        "files": {
            "src/main/java/fixture/ShippingPolicy.java": """package fixture;

public class ShippingPolicy {
    public boolean freeShipping(int total, boolean premium) {
        return total < 100;
    }
}
""",
            "src/test/java/fixture/ShippingPolicyTest.java": """package fixture;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import org.junit.Test;

public class ShippingPolicyTest {
    private final ShippingPolicy policy = new ShippingPolicy();

    @Test
    public void largeOrderIsFree() {
        assertTrue(policy.freeShipping(120, false));
    }

    @Test
    public void smallNonPremiumOrderIsNotFree() {
        assertFalse(policy.freeShipping(99, false));
    }

    @Test
    public void premiumOrderIsFree() {
        assertTrue(policy.freeShipping(50, true));
    }
}
""",
        },
    },
    {
        "task_id": "null-handling",
        "title": "Null handling error",
        "description": "Display-name formatter throws on null and blank names.",
        "problem_statement": "用户显示名称在空值或空白输入时会失败。请修复空值处理并保持正常名称的格式。",
        "tags": ["null-safety", "input-validation"],
        "files": {
            "src/main/java/fixture/NameFormatter.java": """package fixture;

public class NameFormatter {
    public String displayName(String name) {
        return name.trim();
    }
}
""",
            "src/test/java/fixture/NameFormatterTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class NameFormatterTest {
    private final NameFormatter formatter = new NameFormatter();

    @Test
    public void nullBecomesGuest() {
        assertEquals("Guest", formatter.displayName(null));
    }

    @Test
    public void trimsName() {
        assertEquals("Alice", formatter.displayName(" Alice "));
    }

    @Test
    public void blankBecomesGuest() {
        assertEquals("Guest", formatter.displayName(""));
    }
}
""",
        },
    },
    {
        "task_id": "collection-processing",
        "title": "Collection processing error",
        "description": "Order total skips the first collection element.",
        "problem_statement": "订单集合汇总少计算了第一个价格。请修复集合遍历逻辑并确保空集合和普通集合测试通过。",
        "tags": ["collection", "loop"],
        "files": {
            "src/main/java/fixture/OrderTotals.java": """package fixture;

import java.util.List;

public class OrderTotals {
    public int sum(List<Integer> prices) {
        int total = 0;
        for (int i = 1; i < prices.size(); i++) {
            total += prices.get(i);
        }
        return total;
    }
}
""",
            "src/test/java/fixture/OrderTotalsTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import java.util.Arrays;
import java.util.Collections;
import org.junit.Test;

public class OrderTotalsTest {
    private final OrderTotals totals = new OrderTotals();

    @Test
    public void sumsAllPrices() {
        assertEquals(60, totals.sum(Arrays.asList(10, 20, 30)));
    }

    @Test
    public void emptyCollectionIsZero() {
        assertEquals(0, totals.sum(Collections.emptyList()));
    }
}
""",
        },
    },
    {
        "task_id": "string-processing",
        "title": "String processing error",
        "description": "Slug creation fails to trim and normalize separators.",
        "problem_statement": "文章 slug 生成结果保留了空格和大小写，格式不正确。请修复字符串处理实现并确保测试通过。",
        "tags": ["string", "formatting"],
        "files": {
            "src/main/java/fixture/Slugifier.java": """package fixture;

public class Slugifier {
    public String slug(String input) {
        return input;
    }
}
""",
            "src/test/java/fixture/SlugifierTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class SlugifierTest {
    private final Slugifier slugifier = new Slugifier();

    @Test
    public void normalizesSimpleTitle() {
        assertEquals("hello-world", slugifier.slug(" Hello World "));
    }

    @Test
    public void normalizesAlreadySeparatedText() {
        assertEquals("order-service", slugifier.slug("Order-Service"));
    }
}
""",
        },
    },
    {
        "task_id": "off-by-one",
        "title": "Off-by-one paging error",
        "description": "Last page count truncates partial pages.",
        "problem_statement": "分页结果少算了一页，包含不完整尾页时总页数不正确。请修复边界计算并确保分页测试通过。",
        "tags": ["off-by-one", "pagination"],
        "files": {
            "src/main/java/fixture/Paginator.java": """package fixture;

public class Paginator {
    public int lastPage(int totalItems, int pageSize) {
        return totalItems / pageSize;
    }
}
""",
            "src/test/java/fixture/PaginatorTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class PaginatorTest {
    private final Paginator paginator = new Paginator();

    @Test
    public void countsPartialLastPage() {
        assertEquals(4, paginator.lastPage(10, 3));
    }

    @Test
    public void exactMultipleHasNoExtraPage() {
        assertEquals(3, paginator.lastPage(9, 3));
    }
}
""",
        },
    },
    {
        "task_id": "service-business-logic",
        "title": "Service business logic error",
        "description": "Inventory reservation returns the wrong remaining quantity.",
        "problem_statement": "库存预占后剩余数量不正确，且边界库存可能判断错误。请修复服务层业务逻辑并确保测试通过。",
        "tags": ["service", "business-rule"],
        "files": {
            "src/main/java/fixture/InventoryService.java": """package fixture;

public class InventoryService {
    public int reserve(int available, int requested) {
        if (requested > available) {
            throw new IllegalArgumentException("insufficient inventory");
        }
        return available - requested + 1;
    }
}
""",
            "src/test/java/fixture/InventoryServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class InventoryServiceTest {
    private final InventoryService service = new InventoryService();

    @Test
    public void reservesRequestedQuantity() {
        assertEquals(7, service.reserve(10, 3));
    }

    @Test
    public void allowsExactInventory() {
        assertEquals(0, service.reserve(3, 3));
    }

    @Test(expected = IllegalArgumentException.class)
    public void rejectsInsufficientInventory() {
        service.reserve(2, 3);
    }
}
""",
        },
    },
    {
        "task_id": "multi-file-call-graph",
        "title": "Multi-file business calculation error",
        "description": "OrderService delegates through PriceService to a broken Calculator.",
        "problem_statement": "订单价格经过 Service 调用链后计算结果错误。请定位多文件调用链中的缺陷并修复，使 Maven 测试通过。",
        "tags": ["multi-file", "call-graph", "service"],
        "files": {
            "src/main/java/fixture/Calculator.java": """package fixture;

public class Calculator {
    public int multiply(int left, int right) {
        return left + right;
    }
}
""",
            "src/main/java/fixture/PriceService.java": """package fixture;

public class PriceService {
    private final Calculator calculator = new Calculator();

    public int doublePrice(int price) {
        return calculator.multiply(price, 2);
    }
}
""",
            "src/main/java/fixture/OrderService.java": """package fixture;

public class OrderService {
    private final PriceService priceService = new PriceService();

    public int total(int price) {
        return priceService.doublePrice(price);
    }
}
""",
            "src/test/java/fixture/OrderServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class OrderServiceTest {
    @Test
    public void calculatesDoublePrice() {
        assertEquals(6, new OrderService().total(3));
    }
}
""",
        },
    },
    {
        "task_id": "overloaded-method",
        "title": "Overloaded method error",
        "description": "The numeric overload of format applies the wrong transformation.",
        "problem_statement": "格式化器的数字重载输出错误，而字符串重载应保持正常。请修复正确的方法实现并确保测试通过。",
        "tags": ["overload", "method"],
        "files": {
            "src/main/java/fixture/Formatter.java": """package fixture;

public class Formatter {
    public String format(String value) {
        return value;
    }

    public int format(int value) {
        return value - 1;
    }
}
""",
            "src/test/java/fixture/FormatterTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class FormatterTest {
    private final Formatter formatter = new Formatter();

    @Test
    public void keepsStringValue() {
        assertEquals("42", formatter.format("42"));
    }

    @Test
    public void keepsIntegerValue() {
        assertEquals(42, formatter.format(42));
    }
}
""",
        },
    },
    {
        "task_id": "regression-conflict",
        "title": "Regression-sensitive text normalization",
        "description": "Text normalization trims and changes case incorrectly.",
        "problem_statement": "文本标准化同时破坏了大小写和外部空白。请修复实现，确保多个现有行为测试都通过。",
        "tags": ["string", "regression", "multi-assertion"],
        "files": {
            "src/main/java/fixture/TextNormalizer.java": """package fixture;

public class TextNormalizer {
    public String normalize(String input) {
        return input.toLowerCase();
    }
}
""",
            "src/test/java/fixture/TextNormalizerTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class TextNormalizerTest {
    private final TextNormalizer normalizer = new TextNormalizer();

    @Test
    public void trimsOuterWhitespace() {
        assertEquals("Hello", normalizer.normalize("  Hello  "));
    }

    @Test
    public void preservesCamelCase() {
        assertEquals("OrderId", normalizer.normalize("OrderId"));
    }
}
""",
        },
    },
]


def run_git(args: list[str], cwd: Path, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return result.stdout


def build_fixture(item: dict) -> dict:
    fixture_dir = FIXTURES_DIR / item["task_id"]
    if fixture_dir.exists():
        resolved = fixture_dir.resolve()
        if FIXTURES_DIR.resolve() not in resolved.parents:
            raise RuntimeError(f"Refusing to remove unexpected path: {resolved}")
        shutil.rmtree(fixture_dir)

    with tempfile.TemporaryDirectory(prefix="benchmark-fixture-") as temp_dir:
        work = Path(temp_dir)
        (work / "pom.xml").write_text(
            pom(item["task_id"]),
            encoding="utf-8",
            newline="\n",
        )
        for relative, content in item["files"].items():
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_DATE": FIXED_DATE,
        }
        run_git(["init", "-q", "--initial-branch=main"], work, env)
        run_git(["config", "user.name", "Benchmark Fixture"], work, env)
        run_git(["config", "user.email", "benchmark@example.com"], work, env)
        run_git(["config", "core.autocrlf", "false"], work, env)
        run_git(["config", "core.eol", "lf"], work, env)
        run_git(["add", "-A"], work, env)
        run_git(
            ["commit", "-qm", f"benchmark fixture: {item['task_id']}"],
            work,
            env,
        )
        base_commit = run_git(["rev-parse", "HEAD"], work, env).strip()

        fixture_dir.mkdir(parents=True)
        bundle_path = fixture_dir / "repo.bundle"
        run_git(
            ["bundle", "create", str(bundle_path), "HEAD", "main"],
            work,
            env,
        )
        for source in work.rglob("*"):
            if not source.is_file() or ".git" in source.parts:
                continue
            target = fixture_dir / source.relative_to(work)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    return {
        "task_id": item["task_id"],
        "title": item["title"],
        "description": item["description"],
        "repository": f"evaluation/fixtures/{item['task_id']}/repo.bundle",
        "base_commit": base_commit,
        "problem_statement": item["problem_statement"],
        "expected_test_command": "mvn test",
        "timeout": 300,
        "tags": item["tags"],
    }


def main() -> None:
    EVALUATION_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    tasks = [build_fixture(item) for item in FIXTURES]
    TASKS_FILE.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for task in tasks:
        print(f"{task['task_id']} {task['base_commit']}")


if __name__ == "__main__":
    main()

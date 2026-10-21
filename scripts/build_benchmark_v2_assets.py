"""Generate Benchmark V2 fixtures with separate hidden acceptance tests."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = PROJECT_ROOT / "evaluation" / "fixtures" / "v2"
HIDDEN_ROOT = PROJECT_ROOT / "evaluation" / "hidden_tests" / "v2"
TASKS_FILE = PROJECT_ROOT / "evaluation" / "tasks_v2.json"
FIXED_DATE = "2024-01-01T00:00:00+0000"


def pom(artifact: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>dev.java-code-agent.benchmark</groupId>
  <artifactId>{artifact}</artifactId>
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


def support_files(package_prefix: str, count: int) -> dict[str, str]:
    files = {}
    for index in range(count):
        package = f"{package_prefix}.helper{index:02d}"
        files[f"src/main/java/{package.replace('.', '/')}/Support{index:02d}.java"] = f"""package {package};

public class Support{index:02d} {{
    public static int identity(int value) {{
        return value;
    }}
}}
"""
    return files


TASKS = [
    {
        "task_id": "v2-account-repository",
        "title": "Account service and repository withdrawal",
        "description": "Repository and service must enforce balance invariants.",
        "problem_statement": "账户取款后的余额不正确，并且超额取款没有被可靠拒绝。请修复 Service 与 Repository 的协同逻辑。",
        "difficulty": "medium",
        "tags": ["service", "repository", "multi-file"],
        "files": {
            "src/main/java/fixture/InsufficientFundsException.java": """package fixture;

public class InsufficientFundsException extends IllegalArgumentException {
    public InsufficientFundsException(String message) {
        super(message);
    }
}
""",
            "src/main/java/fixture/AccountRepository.java": """package fixture;

public class AccountRepository {
    private int balance = 100;

    public int withdraw(int amount) {
        balance = balance - amount + 1;
        return balance;
    }
}
""",
            "src/main/java/fixture/AccountService.java": """package fixture;

public class AccountService {
    private final AccountRepository repository;

    public AccountService(AccountRepository repository) {
        this.repository = repository;
    }

    public int withdraw(int amount) {
        return repository.withdraw(amount);
    }
}
""",
            "src/test/java/fixture/AccountServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class AccountServiceTest {
    @Test
    public void withdrawalThenOverdraftValidation() {
        AccountService service = new AccountService(new AccountRepository());
        assertEquals(60, service.withdraw(40));
        try {
            service.withdraw(100);
            org.junit.Assert.fail("expected insufficient funds");
        } catch (InsufficientFundsException expected) {
            // expected
        }
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/AccountHiddenTest.java": """package hidden;

import fixture.AccountRepository;
import fixture.AccountService;
import fixture.InsufficientFundsException;
import org.junit.Test;

public class AccountHiddenTest {
    @Test
    public void exactBalanceCanBeWithdrawn() {
        org.junit.Assert.assertEquals(
            0, new AccountService(new AccountRepository()).withdraw(100)
        );
    }

    @Test(expected = InsufficientFundsException.class)
    public void rejectsOverdraft() {
        new AccountService(new AccountRepository()).withdraw(101);
    }
}
""",
        },
    },
    {
        "task_id": "v2-exception-contract",
        "title": "Exception contract across validator and service",
        "description": "Invalid orders must surface the domain exception.",
        "problem_statement": "无效订单目前没有向调用方暴露预期异常，错误处理契约被破坏。请修复验证器与服务之间的异常传播。",
        "difficulty": "medium",
        "tags": ["exception", "service", "multi-file"],
        "files": {
            "src/main/java/fixture/InvalidOrderException.java": """package fixture;

public class InvalidOrderException extends RuntimeException {
    public InvalidOrderException(String message) {
        super(message);
    }
}
""",
            "src/main/java/fixture/OrderValidator.java": """package fixture;

public class OrderValidator {
    public void validate(int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity");
        }
    }
}
""",
            "src/main/java/fixture/OrderService.java": """package fixture;

public class OrderService {
    private final OrderValidator validator = new OrderValidator();

    public boolean submit(int quantity) {
        try {
            validator.validate(quantity);
            return true;
        } catch (RuntimeException ignored) {
            return false;
        }
    }
}
""",
            "src/test/java/fixture/OrderServiceTest.java": """package fixture;

import org.junit.Test;

public class OrderServiceTest {
    @Test(expected = InvalidOrderException.class)
    public void rejectsInvalidOrder() {
        new OrderService().submit(0);
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/OrderExceptionHiddenTest.java": """package hidden;

import fixture.InvalidOrderException;
import fixture.OrderService;
import org.junit.Test;

public class OrderExceptionHiddenTest {
    @Test
    public void acceptsValidOrder() {
        org.junit.Assert.assertTrue(new OrderService().submit(1));
    }

    @Test(expected = InvalidOrderException.class)
    public void rejectsNegativeOrder() {
        new OrderService().submit(-3);
    }
}
""",
        },
    },
    {
        "task_id": "v2-optional-profile",
        "title": "Optional profile fallback",
        "description": "Missing customer names must not throw.",
        "problem_statement": "客户资料缺失时显示名称会失败。请修复 Optional 链和默认值处理。",
        "difficulty": "medium",
        "tags": ["optional", "null-safety", "multi-file"],
        "files": {
            "src/main/java/fixture/CustomerRepository.java": """package fixture;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

public class CustomerRepository {
    private final Map<Integer, String> names = new HashMap<>();

    public CustomerRepository() {
        names.put(1, "Alice");
    }

    public Optional<String> findName(int id) {
        return Optional.ofNullable(names.get(id));
    }
}
""",
            "src/main/java/fixture/ProfileService.java": """package fixture;

public class ProfileService {
    private final CustomerRepository repository;

    public ProfileService(CustomerRepository repository) {
        this.repository = repository;
    }

    public String displayName(int id) {
        return repository.findName(id).get().toUpperCase();
    }
}
""",
            "src/test/java/fixture/ProfileServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class ProfileServiceTest {
    @Test
    public void missingCustomerGetsFallback() {
        assertEquals("GUEST", new ProfileService(new CustomerRepository()).displayName(9));
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/ProfileOptionalHiddenTest.java": """package hidden;

import fixture.CustomerRepository;
import fixture.ProfileService;
import org.junit.Assert;
import org.junit.Test;

public class ProfileOptionalHiddenTest {
    @Test
    public void existingCustomerIsNormalized() {
        Assert.assertEquals(
            "ALICE", new ProfileService(new CustomerRepository()).displayName(1)
        );
    }
}
""",
        },
    },
    {
        "task_id": "v2-stream-filter",
        "title": "Stream filter and aggregation",
        "description": "Regional totals use the wrong predicate.",
        "problem_statement": "区域销售汇总包含了错误的数据。请修复集合 Stream 过滤与聚合逻辑。",
        "difficulty": "medium",
        "tags": ["stream", "collection", "multi-assertion"],
        "files": {
            "src/main/java/fixture/OrderRecord.java": """package fixture;

public class OrderRecord {
    private final String region;
    private final int amount;

    public OrderRecord(String region, int amount) {
        this.region = region;
        this.amount = amount;
    }

    public String getRegion() {
        return region;
    }

    public int getAmount() {
        return amount;
    }
}
""",
            "src/main/java/fixture/ReportService.java": """package fixture;

import java.util.List;

public class ReportService {
    public int totalForRegion(List<OrderRecord> orders, String region) {
        return orders.stream()
            .filter(order -> !order.getRegion().equals(region))
            .mapToInt(OrderRecord::getAmount)
            .sum();
    }
}
""",
            "src/test/java/fixture/ReportServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import java.util.Arrays;
import org.junit.Test;

public class ReportServiceTest {
    @Test
    public void totalsMatchingRegion() {
        assertEquals(
            30,
            new ReportService().totalForRegion(
                Arrays.asList(new OrderRecord("west", 10), new OrderRecord("east", 20)),
                "west"
            )
        );
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/StreamHiddenTest.java": """package hidden;

import fixture.OrderRecord;
import fixture.ReportService;
import java.util.Collections;
import org.junit.Assert;
import org.junit.Test;

public class StreamHiddenTest {
    @Test
    public void missingRegionIsZero() {
        Assert.assertEquals(
            0,
            new ReportService().totalForRegion(Collections.emptyList(), "north")
        );
    }
}
""",
        },
    },
    {
        "task_id": "v2-dto-mapping",
        "title": "Entity to DTO mapping",
        "description": "Mapper loses email and shifts the identifier.",
        "problem_statement": "用户实体转换为 DTO 时字段丢失且 ID 错误。请修复映射逻辑，保证字段完整。",
        "difficulty": "medium",
        "tags": ["dto", "mapping", "multi-file"],
        "files": {
            "src/main/java/fixture/User.java": """package fixture;

public class User {
    private final int id;
    private final String name;
    private final String email;

    public User(int id, String name, String email) {
        this.id = id;
        this.name = name;
        this.email = email;
    }

    public int getId() { return id; }
    public String getName() { return name; }
    public String getEmail() { return email; }
}
""",
            "src/main/java/fixture/UserDto.java": """package fixture;

public class UserDto {
    private final int id;
    private final String name;
    private final String email;

    public UserDto(int id, String name, String email) {
        this.id = id;
        this.name = name;
        this.email = email;
    }

    public int getId() { return id; }
    public String getName() { return name; }
    public String getEmail() { return email; }
}
""",
            "src/main/java/fixture/UserMapper.java": """package fixture;

public class UserMapper {
    public UserDto toDto(User user) {
        return new UserDto(user.getId() + 1, user.getName(), null);
    }
}
""",
            "src/test/java/fixture/UserMapperTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class UserMapperTest {
    @Test
    public void mapsAllFields() {
        UserDto dto = new UserMapper().toDto(new User(7, "Alice", "a@x"));
        assertEquals(7, dto.getId());
        assertEquals("a@x", dto.getEmail());
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/DtoMappingHiddenTest.java": """package hidden;

import fixture.User;
import fixture.UserDto;
import fixture.UserMapper;
import org.junit.Assert;
import org.junit.Test;

public class DtoMappingHiddenTest {
    @Test
    public void preservesEveryField() {
        UserDto dto = new UserMapper().toDto(new User(7, "Alice", "a@x"));
        Assert.assertEquals("Alice", dto.getName());
        Assert.assertEquals("a@x", dto.getEmail());
    }
}
""",
        },
    },
    {
        "task_id": "v2-overload-inheritance",
        "title": "Overload and inheritance pricing",
        "description": "Subclass int overload is wrong while double overload stays inherited.",
        "problem_statement": "会员价格在整数报价重载中出错，但浮点重载应保持正常。请修复继承与重载行为。",
        "difficulty": "medium",
        "tags": ["overload", "inheritance", "method-dispatch"],
        "files": {
            "src/main/java/fixture/BasePriceCalculator.java": """package fixture;

public class BasePriceCalculator {
    public int price(int base) { return base; }
    public double price(double base) { return base; }
}
""",
            "src/main/java/fixture/PremiumPriceCalculator.java": """package fixture;

public class PremiumPriceCalculator extends BasePriceCalculator {
    @Override
    public int price(int base) {
        return base + 10;
    }
}
""",
            "src/main/java/fixture/PriceService.java": """package fixture;

public class PriceService {
    private final PremiumPriceCalculator calculator = new PremiumPriceCalculator();

    public int quote(int base) {
        return calculator.price(base);
    }

    public double quoteDouble(int base) {
        return calculator.price((double) base);
    }
}
""",
            "src/test/java/fixture/PriceServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class PriceServiceTest {
    @Test
    public void premiumDiscountApplies() {
        assertEquals(90, new PriceService().quote(100));
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/OverloadHiddenTest.java": """package hidden;

import fixture.PriceService;
import org.junit.Assert;
import org.junit.Test;

public class OverloadHiddenTest {
    @Test
    public void doubleOverloadRemainsUnchanged() {
        Assert.assertEquals(100.0, new PriceService().quoteDouble(100), 0.001);
    }
}
""",
        },
    },
    {
        "task_id": "v2-order-state-machine",
        "title": "Order state machine",
        "description": "Valid and illegal order transitions are mixed up.",
        "problem_statement": "订单状态流转在支付时进入错误状态，非法流转也没有被阻止。请修复状态机及服务调用。",
        "difficulty": "hard",
        "tags": ["state-machine", "service", "domain"],
        "files": {
            "src/main/java/fixture/OrderState.java": """package fixture;

public enum OrderState { CREATED, PAID, SHIPPED, CANCELLED }
""",
            "src/main/java/fixture/OrderEvent.java": """package fixture;

public enum OrderEvent { PAY, SHIP, CANCEL }
""",
            "src/main/java/fixture/OrderStateMachine.java": """package fixture;

public class OrderStateMachine {
    public OrderState transition(OrderState state, OrderEvent event) {
        if (state == OrderState.CREATED && event == OrderEvent.PAY) {
            return OrderState.CANCELLED;
        }
        if (state == OrderState.PAID && event == OrderEvent.SHIP) {
            return OrderState.SHIPPED;
        }
        if (event == OrderEvent.CANCEL) {
            return OrderState.CANCELLED;
        }
        return state;
    }
}
""",
            "src/main/java/fixture/OrderService.java": """package fixture;

public class OrderService {
    private final OrderStateMachine machine = new OrderStateMachine();

    public OrderState apply(OrderState state, OrderEvent event) {
        return machine.transition(state, event);
    }
}
""",
            "src/test/java/fixture/OrderServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class OrderServiceTest {
    @Test
    public void paidTransition() {
        assertEquals(
            OrderState.PAID,
            new OrderService().apply(OrderState.CREATED, OrderEvent.PAY)
        );
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/StateMachineHiddenTest.java": """package hidden;

import fixture.OrderEvent;
import fixture.OrderService;
import fixture.OrderState;
import org.junit.Test;

public class StateMachineHiddenTest {
    @Test(expected = IllegalStateException.class)
    public void shippedOrderCannotBeCancelled() {
        new OrderService().apply(OrderState.SHIPPED, OrderEvent.CANCEL);
    }

    @Test(expected = IllegalStateException.class)
    public void paidOrderCannotBePaidAgain() {
        new OrderService().apply(OrderState.PAID, OrderEvent.PAY);
    }
}
""",
        },
    },
    {
        "task_id": "v2-equals-hashcode",
        "title": "Equals and hashCode consistency",
        "description": "Domain key implements inconsistent equality.",
        "problem_statement": "客户唯一键在集合中去重失败。请修复 equals/hashCode 契约。",
        "difficulty": "hard",
        "tags": ["equals", "hashcode", "collection"],
        "files": {
            "src/main/java/fixture/CustomerKey.java": """package fixture;

import java.util.Objects;

public class CustomerKey {
    private final int id;
    private final String name;

    public CustomerKey(int id, String name) {
        this.id = id;
        this.name = name;
    }

    @Override
    public boolean equals(Object other) {
        if (!(other instanceof CustomerKey)) return false;
        return id == ((CustomerKey) other).id;
    }

    @Override
    public int hashCode() {
        return Objects.hash(name);
    }
}
""",
            "src/test/java/fixture/CustomerKeyTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import java.util.HashSet;
import java.util.Set;
import org.junit.Test;

public class CustomerKeyTest {
    @Test
    public void duplicateKeysCollapse() {
        Set<CustomerKey> keys = new HashSet<>();
        keys.add(new CustomerKey(1, "Alice"));
        keys.add(new CustomerKey(1, "Alicia"));
        assertEquals(1, keys.size());
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/EqualsHiddenTest.java": """package hidden;

import fixture.CustomerKey;
import org.junit.Assert;
import org.junit.Test;

public class EqualsHiddenTest {
    @Test
    public void equalKeysShareHashCode() {
        Assert.assertEquals(
            new CustomerKey(3, "A").hashCode(),
            new CustomerKey(3, "A").hashCode()
        );
    }

    @Test
    public void differentIdsAreNotEqual() {
        Assert.assertNotEquals(new CustomerKey(1, "A"), new CustomerKey(2, "A"));
    }
}
""",
        },
    },
    {
        "task_id": "v2-cross-package-interface",
        "title": "Cross-package gateway implementation",
        "description": "Checkout service depends on a payment interface implemented in another package.",
        "problem_statement": "支付接口实现计算了错误的手续费，导致跨 package 的结账金额不正确。请修复接口实现并保持服务契约。",
        "difficulty": "hard",
        "tags": ["interface", "cross-package", "service"],
        "files": {
            "src/main/java/com/acme/payment/PaymentGateway.java": """package com.acme.payment;

public interface PaymentGateway {
    int charge(int amount);
}
""",
            "src/main/java/com/acme/payment/DefaultPaymentGateway.java": """package com.acme.payment;

public class DefaultPaymentGateway implements PaymentGateway {
    @Override
    public int charge(int amount) {
        return amount + amount / 100;
    }
}
""",
            "src/main/java/com/acme/checkout/CheckoutService.java": """package com.acme.checkout;

import com.acme.payment.PaymentGateway;

public class CheckoutService {
    private final PaymentGateway gateway;

    public CheckoutService(PaymentGateway gateway) {
        this.gateway = gateway;
    }

    public int checkout(int amount) {
        return gateway.charge(amount);
    }
}
""",
            "src/test/java/com/acme/checkout/CheckoutServiceTest.java": """package com.acme.checkout;

import static org.junit.Assert.assertEquals;
import com.acme.payment.DefaultPaymentGateway;
import org.junit.Test;

public class CheckoutServiceTest {
    @Test
    public void addsPaymentFee() {
        assertEquals(
            1100,
            new CheckoutService(new DefaultPaymentGateway()).checkout(1000)
        );
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/PaymentInterfaceHiddenTest.java": """package hidden;

import com.acme.checkout.CheckoutService;
import com.acme.payment.PaymentGateway;
import org.junit.Assert;
import org.junit.Test;

public class PaymentInterfaceHiddenTest {
    @Test
    public void usesPolymorphicGateway() {
        PaymentGateway gateway = amount -> amount + 5;
        Assert.assertEquals(105, new CheckoutService(gateway).checkout(100));
    }
}
""",
        },
    },
    {
        "task_id": "v2-catalog-layered",
        "title": "Large layered catalog repository",
        "description": "Thirty-plus files across controller, service, pricing, repository, and domain packages.",
        "problem_statement": "商品报价经过 Controller、Service、Repository 多层调用后金额错误。请定位跨层缺陷，修复价格和数量计算。",
        "difficulty": "hard",
        "tags": ["large-project", "controller", "service", "repository", "multi-file", "cross-package"],
        "files": {
            **support_files("com.acme.catalog", 30),
            "src/main/java/com/acme/catalog/domain/Product.java": """package com.acme.catalog.domain;

public class Product {
    private final int price;
    public Product(int price) { this.price = price; }
    public int getPrice() { return price; }
}
""",
            "src/main/java/com/acme/catalog/repository/ProductRepository.java": """package com.acme.catalog.repository;

import com.acme.catalog.domain.Product;
import java.util.HashMap;
import java.util.Map;

public class ProductRepository {
    private final Map<String, Product> products = new HashMap<>();
    public ProductRepository() { products.put("book", new Product(100)); }
    public int findPrice(String id) { return products.get(id).getPrice() - 1; }
}
""",
            "src/main/java/com/acme/catalog/pricing/PricingPolicy.java": """package com.acme.catalog.pricing;

public class PricingPolicy {
    public int tax(int subtotal) { return subtotal / 10; }
}
""",
            "src/main/java/com/acme/catalog/service/CatalogService.java": """package com.acme.catalog.service;

import com.acme.catalog.pricing.PricingPolicy;
import com.acme.catalog.repository.ProductRepository;

public class CatalogService {
    private final ProductRepository repository;
    private final PricingPolicy policy;

    public CatalogService(ProductRepository repository, PricingPolicy policy) {
        this.repository = repository;
        this.policy = policy;
    }

    public int quote(String id, int quantity) {
        int price = repository.findPrice(id);
        return price + policy.tax(price);
    }
}
""",
            "src/main/java/com/acme/catalog/controller/CatalogController.java": """package com.acme.catalog.controller;

import com.acme.catalog.pricing.PricingPolicy;
import com.acme.catalog.repository.ProductRepository;
import com.acme.catalog.service.CatalogService;

public class CatalogController {
    public int quote(String id, int quantity) {
        return new CatalogService(
            new ProductRepository(), new PricingPolicy()
        ).quote(id, quantity);
    }
}
""",
            "src/test/java/com/acme/catalog/controller/CatalogControllerTest.java": """package com.acme.catalog.controller;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CatalogControllerTest {
    @Test
    public void quotesQuantityAndTax() {
        assertEquals(220, new CatalogController().quote("book", 2));
    }
}
""",
            "src/test/java/com/acme/catalog/service/CatalogServiceTest.java": """package com.acme.catalog.service;

import com.acme.catalog.pricing.PricingPolicy;
import com.acme.catalog.repository.ProductRepository;
import org.junit.Assert;
import org.junit.Test;

public class CatalogServiceTest {
    @Test
    public void quotesSingleUnit() {
        Assert.assertEquals(
            110,
            new CatalogService(new ProductRepository(), new PricingPolicy()).quote("book", 1)
        );
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/CatalogHiddenTest.java": """package hidden;

import com.acme.catalog.controller.CatalogController;
import org.junit.Assert;
import org.junit.Test;

public class CatalogHiddenTest {
    @Test
    public void handlesLargerQuantity() {
        Assert.assertEquals(330, new CatalogController().quote("book", 3));
    }
}
""",
        },
    },
    {
        "task_id": "v2-checkout-transaction",
        "title": "Checkout repository transaction",
        "description": "Checkout coordinates inventory and order persistence.",
        "problem_statement": "结账时库存与订单状态不一致，且库存边界可能越界。请修复 Repository 与 Service 的协同。",
        "difficulty": "hard",
        "tags": ["transaction", "repository", "service", "multi-file"],
        "files": {
            "src/main/java/fixture/InventoryRepository.java": """package fixture;

public class InventoryRepository {
    private int stock = 10;
    public int reserve(int quantity) {
        stock = stock - quantity + 1;
        return stock;
    }
}
""",
            "src/main/java/fixture/OrderRepository.java": """package fixture;

public class OrderRepository {
    private int saved;
    public void save(String orderId) { saved++; }
    public int savedCount() { return saved; }
}
""",
            "src/main/java/fixture/CheckoutService.java": """package fixture;

public class CheckoutService {
    private final InventoryRepository inventory;
    private final OrderRepository orders;

    public CheckoutService(InventoryRepository inventory, OrderRepository orders) {
        this.inventory = inventory;
        this.orders = orders;
    }

    public int checkout(String orderId, int quantity) {
        int remaining = inventory.reserve(quantity);
        orders.save(orderId);
        return remaining;
    }
}
""",
            "src/test/java/fixture/CheckoutServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CheckoutServiceTest {
    @Test
    public void reservesStockAndSavesOrder() {
        OrderRepository orders = new OrderRepository();
        int remaining = new CheckoutService(new InventoryRepository(), orders)
            .checkout("o1", 3);
        assertEquals(7, remaining);
        assertEquals(1, orders.savedCount());
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/CheckoutHiddenTest.java": """package hidden;

import fixture.CheckoutService;
import fixture.InventoryRepository;
import fixture.OrderRepository;
import org.junit.Test;

public class CheckoutHiddenTest {
    @Test(expected = IllegalStateException.class)
    public void rejectsOrderAboveStock() {
        new CheckoutService(new InventoryRepository(), new OrderRepository())
            .checkout("o2", 11);
    }
}
""",
        },
    },
    {
        "task_id": "v2-regression-pipeline",
        "title": "Regression-sensitive text pipeline",
        "description": "Two services must preserve distinct normalization contracts.",
        "problem_statement": "文本处理流水线破坏了 slug 分隔符和展示文本大小写。请修复多个组件且避免引入回归。",
        "difficulty": "hard",
        "tags": ["regression", "string", "multi-file", "multi-assertion"],
        "files": {
            "src/main/java/fixture/SlugService.java": """package fixture;

public class SlugService {
    public String slug(String value) {
        return value.trim();
    }
}
""",
            "src/main/java/fixture/DisplayService.java": """package fixture;

public class DisplayService {
    public String display(String value) {
        return value.toLowerCase();
    }
}
""",
            "src/main/java/fixture/ContentPipeline.java": """package fixture;

public class ContentPipeline {
    private final SlugService slugs = new SlugService();
    private final DisplayService display = new DisplayService();

    public String slug(String value) { return slugs.slug(value); }
    public String display(String value) { return display.display(value); }
}
""",
            "src/test/java/fixture/ContentPipelineTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class ContentPipelineTest {
    private final ContentPipeline pipeline = new ContentPipeline();

    @Test
    public void transformsSlugThenPreservesDisplayCase() {
        assertEquals("hello-world", pipeline.slug(" Hello World "));
        assertEquals("OrderId", pipeline.display("OrderId"));
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/PipelineRegressionHiddenTest.java": """package hidden;

import fixture.ContentPipeline;
import org.junit.Assert;
import org.junit.Test;

public class PipelineRegressionHiddenTest {
    @Test
    public void displayTrimsOuterWhitespace() {
        Assert.assertEquals("Hello", new ContentPipeline().display("  Hello  "));
    }
}
""",
        },
    },
    {
        "task_id": "v2-overloaded-router",
        "title": "Overloaded notification router",
        "description": "String and numeric overloads choose different channels.",
        "problem_statement": "通知路由器对字符串与数字重载的处理不一致。请修复重载实现并保证服务层调用正确。",
        "difficulty": "hard",
        "tags": ["overload", "service", "multi-assertion"],
        "files": {
            "src/main/java/fixture/MessageRouter.java": """package fixture;

public class MessageRouter {
    public String route(String value) {
        return "text:" + value;
    }

    public String route(Integer value) {
        return "text:" + value;
    }
}
""",
            "src/main/java/fixture/NotificationService.java": """package fixture;

public class NotificationService {
    private final MessageRouter router = new MessageRouter();
    public String notifyText(String value) { return router.route(value); }
    public String notifyNumber(Integer value) { return router.route(value); }
}
""",
            "src/test/java/fixture/NotificationServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class NotificationServiceTest {
    @Test
    public void numericOverloadUsesNumericChannel() {
        assertEquals("number:42", new NotificationService().notifyNumber(42));
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/OverloadedRouterHiddenTest.java": """package hidden;

import fixture.NotificationService;
import org.junit.Assert;
import org.junit.Test;

public class OverloadedRouterHiddenTest {
    @Test
    public void textOverloadRemainsText() {
        Assert.assertEquals("text:hello", new NotificationService().notifyText("hello"));
    }
}
""",
        },
    },
    {
        "task_id": "v2-optional-repository-chain",
        "title": "Optional repository chain",
        "description": "Customer and address repositories compose through Optional.",
        "problem_statement": "客户与地址仓库组合查询时会丢失默认值并产生错误 DTO。请修复 Optional 链与字段映射。",
        "difficulty": "hard",
        "tags": ["optional", "repository", "dto", "multi-file"],
        "files": {
            "src/main/java/fixture/CustomerRepository.java": """package fixture;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

public class CustomerRepository {
    private final Map<Integer, String> names = new HashMap<>();
    public CustomerRepository() { names.put(1, "Alice"); }
    public Optional<String> findName(int id) { return Optional.ofNullable(names.get(id)); }
}
""",
            "src/main/java/fixture/AddressRepository.java": """package fixture;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

public class AddressRepository {
    private final Map<Integer, String> cities = new HashMap<>();
    public AddressRepository() { cities.put(1, "Shanghai"); }
    public Optional<String> findCity(int id) { return Optional.ofNullable(cities.get(id)); }
}
""",
            "src/main/java/fixture/CustomerDto.java": """package fixture;

public class CustomerDto {
    public final String name;
    public final String city;
    public CustomerDto(String name, String city) {
        this.name = name;
        this.city = city;
    }
}
""",
            "src/main/java/fixture/CustomerService.java": """package fixture;

public class CustomerService {
    private final CustomerRepository customers;
    private final AddressRepository addresses;

    public CustomerService(CustomerRepository customers, AddressRepository addresses) {
        this.customers = customers;
        this.addresses = addresses;
    }

    public CustomerDto load(int id) {
        String name = customers.findName(id).get();
        String city = addresses.findCity(id).get();
        return new CustomerDto(name, city);
    }
}
""",
            "src/test/java/fixture/CustomerServiceTest.java": """package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CustomerServiceTest {
    @Test
    public void missingAddressGetsDefault() {
        CustomerDto dto = new CustomerService(new CustomerRepository(), new AddressRepository()).load(2);
        assertEquals("Unknown", dto.name);
        assertEquals("Unknown", dto.city);
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/OptionalRepositoryHiddenTest.java": """package hidden;

import fixture.AddressRepository;
import fixture.CustomerRepository;
import fixture.CustomerService;
import org.junit.Assert;
import org.junit.Test;

public class OptionalRepositoryHiddenTest {
    @Test
    public void existingCustomerIsMapped() {
        Assert.assertEquals(
            "Shanghai",
            new CustomerService(new CustomerRepository(), new AddressRepository()).load(1).city
        );
    }
}
""",
        },
    },
    {
        "task_id": "v2-inventory-layered",
        "title": "Large inventory management platform",
        "description": "Thirty-plus files with controller-service-repository flow and hidden stock invariants.",
        "problem_statement": "库存平台在多层调用中返回错误库存，并可能允许超卖。请定位并修复 Repository、Service 和业务边界。",
        "difficulty": "hard",
        "tags": ["large-project", "controller", "service", "repository", "multi-file", "state-machine"],
        "files": {
            **support_files("com.acme.inventory", 32),
            "src/main/java/com/acme/inventory/domain/StockItem.java": """package com.acme.inventory.domain;

public class StockItem {
    private final int available;
    public StockItem(int available) { this.available = available; }
    public int getAvailable() { return available; }
}
""",
            "src/main/java/com/acme/inventory/repository/StockRepository.java": """package com.acme.inventory.repository;

import com.acme.inventory.domain.StockItem;
import java.util.HashMap;
import java.util.Map;

public class StockRepository {
    private final Map<String, Integer> stock = new HashMap<>();
    public StockRepository() { stock.put("sku-1", 10); }
    public int reserve(String id, int qty) {
        int remaining = stock.get(id) - qty + 1;
        stock.put(id, remaining);
        return remaining;
    }
    public StockItem find(String id) { return new StockItem(stock.get(id)); }
}
""",
            "src/main/java/com/acme/inventory/service/InventoryService.java": """package com.acme.inventory.service;

import com.acme.inventory.repository.StockRepository;

public class InventoryService {
    private final StockRepository repository;
    public InventoryService(StockRepository repository) { this.repository = repository; }
    public int reserve(String id, int qty) {
        return repository.reserve(id, qty);
    }
}
""",
            "src/main/java/com/acme/inventory/controller/InventoryController.java": """package com.acme.inventory.controller;

import com.acme.inventory.repository.StockRepository;
import com.acme.inventory.service.InventoryService;

public class InventoryController {
    private final InventoryService service = new InventoryService(new StockRepository());
    public int reserve(String id, int qty) { return service.reserve(id, qty); }
}
""",
            "src/test/java/com/acme/inventory/controller/InventoryControllerTest.java": """package com.acme.inventory.controller;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class InventoryControllerTest {
    @Test
    public void reservesStock() {
        assertEquals(7, new InventoryController().reserve("sku-1", 3));
    }
}
""",
            "src/test/java/com/acme/inventory/service/InventoryServiceTest.java": """package com.acme.inventory.service;

import com.acme.inventory.repository.StockRepository;
import org.junit.Assert;
import org.junit.Test;

public class InventoryServiceTest {
    @Test
    public void exactStockCanBeReserved() {
        Assert.assertEquals(0, new InventoryService(new StockRepository()).reserve("sku-1", 10));
    }
}
""",
        },
        "hidden_files": {
            "src/test/java/hidden/InventoryHiddenTest.java": """package hidden;

import com.acme.inventory.controller.InventoryController;
import org.junit.Test;

public class InventoryHiddenTest {
    @Test(expected = IllegalStateException.class)
    public void cannotOversell() {
        new InventoryController().reserve("sku-1", 11);
    }
}
""",
        },
    },
]


def git(args: list[str], cwd: Path, env: dict | None = None) -> str:
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


def write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def build_task(item: dict) -> dict:
    task_id = item["task_id"]
    fixture_dir = FIXTURES_ROOT / task_id
    hidden_dir = HIDDEN_ROOT / task_id
    for directory in (fixture_dir, hidden_dir):
        if directory.exists():
            resolved = directory.resolve()
            allowed = (
                FIXTURES_ROOT.resolve()
                if directory == fixture_dir
                else HIDDEN_ROOT.resolve()
            )
            if allowed not in resolved.parents:
                raise RuntimeError(f"Unexpected path: {resolved}")
            shutil.rmtree(directory)

    with tempfile.TemporaryDirectory(prefix="benchmark-v2-") as temp_dir:
        work = Path(temp_dir)
        (work / "pom.xml").write_text(
            pom(task_id), encoding="utf-8", newline="\n"
        )
        write_tree(work, item["files"])
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_DATE": FIXED_DATE,
        }
        git(["init", "-q", "--initial-branch=main"], work, env)
        git(["config", "user.name", "Benchmark Fixture"], work, env)
        git(["config", "user.email", "benchmark@example.com"], work, env)
        git(["config", "core.autocrlf", "false"], work, env)
        git(["config", "core.eol", "lf"], work, env)
        git(["add", "-A"], work, env)
        git(["commit", "-qm", f"benchmark v2 fixture: {task_id}"], work, env)
        base_commit = git(["rev-parse", "HEAD"], work, env).strip()

        fixture_dir.mkdir(parents=True)
        bundle_path = fixture_dir / "repo.bundle"
        git(["bundle", "create", str(bundle_path), "HEAD", "main"], work, env)
        for source in work.rglob("*"):
            if not source.is_file() or ".git" in source.parts:
                continue
            target = fixture_dir / source.relative_to(work)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    hidden_dir.mkdir(parents=True)
    write_tree(hidden_dir, item["hidden_files"])
    return {
        "task_id": task_id,
        "title": item["title"],
        "description": item["description"],
        "repository": f"evaluation/fixtures/v2/{task_id}/repo.bundle",
        "base_commit": base_commit,
        "problem_statement": item["problem_statement"],
        "expected_test_command": "mvn test",
        "timeout": 300,
        "tags": item["tags"],
        "suite": "v2",
        "difficulty": item["difficulty"],
        "hidden_tests": f"evaluation/hidden_tests/v2/{task_id}",
    }


def main() -> None:
    FIXTURES_ROOT.mkdir(parents=True, exist_ok=True)
    HIDDEN_ROOT.mkdir(parents=True, exist_ok=True)
    tasks = [build_task(item) for item in TASKS]
    TASKS_FILE.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for task in tasks:
        print(
            f"{task['task_id']} [{task['difficulty']}] {task['base_commit']}"
        )


if __name__ == "__main__":
    main()

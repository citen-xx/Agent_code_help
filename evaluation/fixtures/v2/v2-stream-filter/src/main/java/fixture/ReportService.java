package fixture;

import java.util.List;

public class ReportService {
    public int totalForRegion(List<OrderRecord> orders, String region) {
        return orders.stream()
            .filter(order -> !order.getRegion().equals(region))
            .mapToInt(OrderRecord::getAmount)
            .sum();
    }
}

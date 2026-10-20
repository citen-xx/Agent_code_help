package fixture;

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

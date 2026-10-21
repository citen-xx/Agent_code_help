package fixture;

public class PriceService {
    private final PremiumPriceCalculator calculator = new PremiumPriceCalculator();

    public int quote(int base) {
        return calculator.price(base);
    }

    public double quoteDouble(int base) {
        return calculator.price((double) base);
    }
}
